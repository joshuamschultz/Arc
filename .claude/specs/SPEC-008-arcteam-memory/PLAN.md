# PLAN: ArcTeam Memory (SPEC-008) — Phase 1 (Core Service)

**Status**: COMPLETE
**Spec**: SPEC-008
**Phase**: 1 of 4
**Estimated Files**: 11 new, 1 modified

---

## Implementation Phases

### Phase 1A: Foundation (Config + Types + Errors + Scaffold)

- [x] **1A.1** Create `arcteam/memory/` package scaffold
  - `__init__.py` — public API exports
  - `config.py` — `TeamMemoryConfig` (Pydantic BaseModel)
  - `errors.py` — `TeamMemoryError`, `EntityNotFoundError`, `EntityValidationError`, `ClassificationError`, `IndexCorruptionError`, `PromotionError`, `LockTimeoutError`
  - Test: `tests/unit/memory/test_config.py`

- [x] **1A.2** Create `arcteam/memory/types.py`
  - `Classification(IntEnum)` — UNCLASSIFIED, CUI, CONFIDENTIAL, SECRET, TOP_SECRET
  - `EntityMetadata(BaseModel)` — YAML frontmatter schema
  - `IndexEntry(BaseModel)` — _index.json entry
  - `SearchResult(BaseModel)` — search result
  - `EntityFile(BaseModel)` — metadata + content
  - `PromotionResult(BaseModel)` — promote() return
  - `MemoryStatus(BaseModel)` — status snapshot
  - Test: `tests/unit/memory/test_types.py`

### Phase 1B: Storage Layer (MemoryStorage)

- [x] **1B.1** Implement `arcteam/memory/storage.py`
  - `MemoryStorage` class with `read_entity()`, `write_entity()`, `delete_entity()`, `read_frontmatter_only()`, `list_entity_files()`
  - YAML frontmatter I/O via `python-frontmatter`
  - Atomic writes via `tempfile.mkstemp` + `os.replace` (match existing `FileBackend` pattern)
  - `fcntl.flock` for write locking with timeout/retry
  - `asyncio.to_thread` for all sync I/O
  - Entity path resolution: `entities/{entity_type}/{entity_id}.md`
  - Token estimation: `len(content.split()) * 1.3`
  - Test: `tests/unit/memory/test_storage.py`

### Phase 1C: Index Management

- [x] **1C.1** Implement `arcteam/memory/index_manager.py`
  - `IndexManager` with `get_index()`, `lookup()`, `entity_exists()`, `get_backlinks()`, `touch_dirty()`, `rebuild()`
  - Dirty flag via `.dirty` marker file (idempotent create/check)
  - `get_index()`: check dirty → rebuild if needed → cache in memory
  - `rebuild()`: read frontmatter only from all `.md` files, compute `linked_from` from all `links_to`, atomic write `_index.json`
  - `get_backlinks()`: scan cached index for entities whose `links_to` contains target
  - Federal tier: integrity checksum on index load
  - Test: `tests/unit/memory/test_index_manager.py`

### Phase 1D: Search Engine (BM25 + Traversal)

- [x] **1D.1** Implement `arcteam/memory/search_engine.py`
  - `SearchEngine` with `search()`
  - `_build_corpus()`: strip frontmatter, markdown syntax, code blocks; keep wiki-link text; tokenize
  - `_bm25_search()`: `BM25Okapi` scoring via `rank-bm25`; return `(entity_id, score)` pairs
  - `_traverse_links()`: BFS with `visited: set[str]`, threshold = `0.3 * max_score`, prune branches below threshold, max_hops from config
  - `_tokenize()`: lowercase, split on `\s+|[^\w-]`
  - `_strip_markdown()`: remove `---` blocks, `#`, `*`, `` ` ``, fenced code blocks
  - Corpus caching: tokenized corpus cached, invalidated on dirty flag
  - Empty query → return empty list (no BM25 scoring)
  - Test: `tests/unit/memory/test_search_engine.py`

### Phase 1E: Security (Classification + Promotion Gate)

- [x] **1E.1** Implement `arcteam/memory/classification.py`
  - `ClassificationChecker` with `check_access()`, `filter_results()`, `parse_classification()`
  - Tier-gated enforcement: federal=hard block, enterprise=warn+block, personal=no enforcement
  - Silent filtering — never reveal existence of filtered results
  - Audit-log all denials via `AuditLogger`
  - Test: `tests/unit/memory/test_classification.py`

- [x] **1E.2** Implement `arcteam/memory/promotion_gate.py`
  - `PromotionGate` with `promote()`
  - `_validate()`: Pydantic schema check on metadata
  - `_check_duplicate()`: index lookup for existing entity
  - Classification label enforcement (tier-gated)
  - UNCLASSIFIED: write immediately via `MemoryStorage.write_entity()` + `IndexManager.touch_dirty()`
  - CUI+: queue for approval via `MessagingService.send()` to `memory-approval` channel
  - Audit-log all promotions (success, failure, queued)
  - Test: `tests/unit/memory/test_promotion_gate.py`

### Phase 1F: Service Facade + CLI

- [x] **1F.1** Implement `arcteam/memory/service.py` (facade)
  - `TeamMemoryService` with `search()`, `promote()`, `get_entity()`, `list_entities()`, `record_decision()`, `status()`
  - Constructor wires all components: MemoryStorage, IndexManager, SearchEngine, PromotionGate, ClassificationChecker
  - Null Object pattern: when `config.enabled = False`, all methods return empty/no-op
  - `record_decision()`: delegates to `StorageBackend.append()` for decisions JSONL
  - `status()`: returns `MemoryStatus` from IndexManager + filesystem
  - Test: `tests/unit/memory/test_service.py`

- [x] **1F.2** Implement `arcteam/memory/cli.py`
  - CLI commands: `status`, `search`, `entity show`, `entity list`, `index rebuild`, `promote`
  - Follows existing pattern from `arcteam/cli.py`
  - Test: `tests/unit/memory/test_cli.py`

- [x] **1F.3** Update `arcteam/__init__.py` to export memory API
  - Add `TeamMemoryService`, `TeamMemoryConfig`, key types to `__all__`

### Phase 1G: Integration Tests

- [x] **1G.1** Integration test: full service lifecycle
  - Test: `tests/integration/memory/test_team_memory_service.py`
  - Create TeamMemoryService with FileBackend + MemoryStorage
  - Promote entities (create + update)
  - Search returns relevant results
  - Wiki-link traversal discovers connected entities
  - Classification filtering works silently
  - Null Object pattern when disabled
  - Index rebuild produces correct manifest
  - Decisions JSONL append works

- [x] **1G.2** Integration test: concurrency
  - Test: `tests/integration/memory/test_team_memory_concurrency.py`
  - Concurrent promotes to different entities (should both succeed)
  - Concurrent promotes to same entity (flock serializes)
  - Concurrent search + promote (reads lock-free)
  - Index rebuild under concurrent writes (dirty flag idempotent)

---

## File Inventory

### New Files (11)

| File | LOC Est | Purpose |
|------|---------|---------|
| `arcteam/memory/__init__.py` | ~25 | Public API exports |
| `arcteam/memory/config.py` | ~35 | TeamMemoryConfig |
| `arcteam/memory/types.py` | ~90 | Classification, EntityMetadata, SearchResult, etc. |
| `arcteam/memory/errors.py` | ~35 | Error hierarchy |
| `arcteam/memory/storage.py` | ~140 | MemoryStorage (YAML + markdown I/O) |
| `arcteam/memory/index_manager.py` | ~120 | IndexManager (_index.json lifecycle) |
| `arcteam/memory/search_engine.py` | ~180 | SearchEngine (BM25 + wiki-link traversal) |
| `arcteam/memory/classification.py` | ~60 | ClassificationChecker |
| `arcteam/memory/promotion_gate.py` | ~120 | PromotionGate |
| `arcteam/memory/service.py` | ~150 | TeamMemoryService (facade) |
| `arcteam/memory/cli.py` | ~100 | CLI commands |

**Total new LOC**: ~1,055 estimated

### Modified Files (1)

| File | Change |
|------|--------|
| `arcteam/__init__.py` | Add memory exports to `__all__` |

### Test Files (11)

| File | Type |
|------|------|
| `tests/unit/memory/test_config.py` | Unit |
| `tests/unit/memory/test_types.py` | Unit |
| `tests/unit/memory/test_storage.py` | Unit |
| `tests/unit/memory/test_index_manager.py` | Unit |
| `tests/unit/memory/test_search_engine.py` | Unit |
| `tests/unit/memory/test_classification.py` | Unit |
| `tests/unit/memory/test_promotion_gate.py` | Unit |
| `tests/unit/memory/test_service.py` | Unit |
| `tests/unit/memory/test_cli.py` | Unit |
| `tests/integration/memory/test_team_memory_service.py` | Integration |
| `tests/integration/memory/test_team_memory_concurrency.py` | Integration |

---

## Verification Checklist

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] `ruff check arcteam/memory/` — 0 errors
- [ ] `mypy arcteam/memory/ --strict` — 0 errors
- [ ] Existing `arcteam/` tests still pass (no regression)
- [ ] TeamMemoryService loads and functions standalone
- [ ] Entity files created with valid YAML frontmatter
- [ ] `_index.json` built correctly, dirty flag works
- [ ] BM25 search returns relevant results
- [ ] Wiki-link traversal follows connections, respects max_hops
- [ ] Adaptive stopping prunes low-relevance branches
- [ ] Promotion gate validates schema, enforces classification (tier-gated)
- [ ] Classification access control silently filters results
- [ ] Audit events emitted for all memory operations
- [ ] File locks serialize concurrent writes
- [ ] Lock-free reads work under concurrent access
- [ ] Null Object pattern returns empty results when disabled
- [ ] CLI commands functional
- [ ] Zero imports from arcagent — fully standalone

---

## Dependencies

### New External Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| `rank-bm25` | >=0.2.2 | BM25Okapi scoring |
| `python-frontmatter` | >=1.0 | YAML frontmatter + markdown I/O |

### Existing (reused)

- `pydantic` (existing in arcteam)
- `opentelemetry` (existing in arcteam)
- `fcntl` (stdlib)
- `asyncio` (stdlib)

---

## Risks

| Risk | Mitigation |
|------|------------|
| BM25 performance at scale (>1000 entities) | Phase 3 adds vector search fallback; Phase 1 targets <1000 files |
| Wiki-link injection (crafted entity names) | Only follow links to entities in `_index.json`; Pydantic validation on entity_id |
| Classification label missing on promote | Tier-gated enforcement; federal blocks, enterprise warns |
| Index corruption from concurrent rebuilds | Atomic write (tempfile + os.replace); last writer wins (both correct) |
| rank-bm25 dependency adds supply chain surface | Pure Python, well-maintained, ~50 LOC; can inline if needed |
| python-frontmatter YAML injection | PyYAML safe_load (default); no unsafe deserialization |
| NFS file locking | `fcntl.flock` doesn't work on NFS; document as local-filesystem-only for Phase 1 |

---

## Task Summary

**Total**: 13 tasks
**Completed**: 13/13
**Remaining**: 0
