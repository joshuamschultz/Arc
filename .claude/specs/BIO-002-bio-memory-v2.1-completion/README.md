# BIO-002: Bio-Memory v2.1 Completion

| Field | Value |
|-------|-------|
| **ID** | BIO-002 |
| **Feature** | Complete ARC Memory System v2.1 — entity format, team wiring, light consolidation, deep consolidation |
| **Type** | Integration (module extension) |
| **Status** | REVIEWED |
| **Created** | 2026-02-25 |
| **Priority** | High |
| **Prerequisite** | BIO-001 (COMPLETE) |
| **Design Doc** | `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` |
| **Implementation Notes** | `PLAN-memory-v2.1-implementation.md` (root) |

## Summary

Completes the bio_memory module to full v2.1 spec coverage. BIO-001 implemented ~20/68 requirements (working memory, identity, basic episodes, basic retrieval, basic light consolidation). This spec covers the remaining gaps:

- **Gap 6**: Entity format — retriever searches `workspace/entities/`, wiki-link resolution across entity graph
- **Gap 2**: arcagent-arcteam wiring — lazy TeamMemoryService integration, team entity search, promotion gate
- **Gap 3**: Complete light consolidation — LC-4 through LC-7 (entity updates, corrections, co-occurrence linking, new entity stubs)
- **Gap 4**: Deep consolidation engine — entity-centric pass, graph-centric pass, merge detection, staleness, identity refresh

**Explicitly excluded**: Gap 5 (automatic retrieval) — user wants LLM-driven retrieval via `memory_search` tool calls only.

## Key Decisions

- **arcteam is optional** — all arcteam imports are lazy (inside methods). If arcteam isn't installed, team features silently degrade. (D-029, A-004)
- **Entities at workspace/entities/** — NOT inside memory/. Matches current LLM-created entity behavior. (A-006)
- **Lazy entity normalization** — files get v2.1 frontmatter only when consolidation touches them. No migration needed. (DP-001)
- **Single LLM call for entity analysis** — LC-4/5/6/7 use one LLM call per session. Cost-efficient.
- **Deep consolidation is manual/scheduled** — not automatic. Triggered via tool, CLI, or scheduler. (I-003)
- **Retriever gets workspace path** — needed to access workspace/entities/ alongside memory/.
- **`linked_from` NOT stored in files** — computed from `_index.json` at read-time. Eliminates cross-file write consistency problems. (D-021, wiki-link research)
- **Content-hash gating** — skip unchanged entities in deep consolidation. 80-90% cost reduction. (SimpleMem, Bio-Memory /deepen)
- **Write-ahead manifest** — crash safety for multi-file deep consolidation. (Zep/Graphiti pattern)
- **Entity registry defense** — rate-limit entity creation (3/session) and links (10/session). Only follow links to existing files. (Security research)
- **Two-gate significance** — deterministic pre-filter before LLM judgment. (SimpleMem entropy research)
- **Sequential entity processing** — avoid coordination complexity during deep consolidation. (LLM consolidation research)

## Traceability

| PRD Requirement | SDD Component | PLAN Phase |
|-----------------|---------------|------------|
| RT-6, EG-* | Retriever entity scope | Phase 1 |
| PG-1..4 | TeamMemoryService wiring | Phase 2 |
| LC-4..7 | Consolidator entity updates | Phase 3 |
| DC-1..15 | DeepConsolidator | Phase 4 |
| EL-1..6 | Entity lifecycle | Phase 3-4 |

## Review Findings (2026-02-25)

### Quality Gates — ALL PASS

| Gate | Result | Threshold |
|------|--------|-----------|
| Tests | 207 pass | 0 failures |
| Coverage | 84% | >= 80% |
| Ruff | 0 errors | 0 |
| Critical vulns | 0 | 0 |

### Fixes Applied During Review

1. **Security HIGH-01**: Added `_validate_path` to DeepConsolidator (workspace bounds check)
2. **Security MEDIUM-03**: Rate limits on graph links (20/run) and merge evaluations (10/run)
3. **Security MEDIUM-04**: Boundary tags + anti-injection on entity rewrite and merge prompts
4. **Security LOW-04**: `sanitize_wiki_link` on entity_type before path construction
5. **FINDING-09**: Self-link prevention in co-occurrence linking
6. **FINDING-10**: try/except around graph pass link additions
7. **Coverage**: 28 new tests in test_bio_memory_module.py (59% → 94%)
8. **CLI**: Replaced `unittest.mock.MagicMock` with proper `_NoOpTelemetry` stub

### Tech Debt (Non-blocking, logged for future)

| ID | Priority | Description |
|----|----------|-------------|
| TD-1 | Medium | Extract shared entity_ops.py (validate_path, resolve_entity, add_link, wiki regex) — DRY between consolidator.py and deep_consolidator.py |
| TD-3 | Low | Implement _resume_from_manifest for crash recovery |
| TD-5 | Low | Add LLM call timeouts (OWASP LLM10) |
| P-001 | Medium | ConsolidationContext cache for frontmatter + episode content (80-90% FS reduction) |
| P-005 | Medium | Cache rotation state in memory during consolidation run |
| P-015 | Low | O(n^2) → inverted index for merge candidates |
| TD-016 | High | Add LLM call timeouts + cumulative token ceiling (OWASP LLM10) |
| TD-009 | High | Integration tests for consolidation pipeline end-to-end |
| TD-013 | Medium | Update MODULE.yaml emits list (severely stale, 15+ missing events) |
| TD-015 | Medium | _find_episodes_referencing uses substring match, should use wiki-link regex |
| TD-020 | Medium | per_entity_budget unit confusion: words vs tokens inconsistency |

## Learnings

- DeepConsolidator's `_resolve_entity` and `_find_touched_entities` must validate paths against workspace bounds — same as Consolidator. Pattern emerged after the fact because DeepConsolidator was developed later.
- LLM prompt boundary tags need to be applied consistently across ALL prompts, including merge confirmation. Easy to miss secondary prompts.
- `bio_memory_module.py` was the main coverage gap (53%) because bus handlers and tool handlers weren't tested. Adding 28 tests brought it to 94%.
- MagicMock in production CLI code is a code smell. A 5-line `_NoOpTelemetry` class eliminates the `unittest.mock` dependency.

## Research Sources Referenced

- **Decisions log** (`.claude/decisions-log.md`): 24 bio-memory decisions (A-001..008, D-001..004, T-001, O-001, S-001..002, I-001..003, P-001..003, E-001..002), 35 arcteam-memory decisions (D-018..035)
- **Bio-Memory /deepen** (2026-02-21): 3 parallel agents, 50+ papers — memory poisoning, grep-based retrieval, LLM consolidation patterns
- **arcteam-memory /deepen** (2026-02-21): 6 parallel agents — BM25, wiki-link traversal, YAML/markdown I/O, LLM consolidation, classification, concurrency
- **Key papers**: SimpleMem (entropy gating), Hindsight (belief confidence), Zep/Graphiti (bi-temporal, 5-prompt link discovery), MINJA (memory injection), MAIF (Ed25519 signatures), GrepRAG (grep vs vector benchmarks)

## Solutions Referenced

- BIO-001 patterns (background task management, LLM output sanitization, boundary markers)
