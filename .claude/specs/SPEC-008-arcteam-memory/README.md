# SPEC-008: ArcTeam Memory

| Field | Value |
|-------|-------|
| **ID** | SPEC-008 |
| **Feature** | Shared team knowledge graph for ArcTeam |
| **Type** | Integration (library service) |
| **Status** | PENDING |
| **Created** | 2026-02-21 |
| **Priority** | High |
| **Package** | `packages/arcteam/` |
| **Build Decisions** | `.claude/decisions-log.md` (ArcTeam Memory section, 31 decisions) |
| **Research** | `/deepen` completed (6 research areas, 50+ sources) |
| **Design Doc** | `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` (Section 7) |

## Summary

Team memory is the shared knowledge graph — "Confluence for the team." Wiki-linked markdown entity files with YAML frontmatter, searchable via BM25 with adaptive graph traversal. `TeamMemoryService` is a standalone service in arcteam, framework-agnostic (usable by arcagent, langchain, crewai, etc.). Agents read freely; writes go through a promotion gate with classification validation. Consolidation is LLM-driven via arcllm.

This spec covers **Phase 1** (core service) only. Consolidation (Phase 2), scale (Phase 3), and hardening (Phase 4) will be specified incrementally.

## Key Decisions

- **D-010**: Shared knowledge base — independent from per-agent memory
- **D-011**: Existing `StorageBackend` for decisions JSONL; new `MemoryStorage` for entity markdown
- **D-012**: Entity files are `.md` with YAML frontmatter (human-readable, git-diffable)
- **D-013**: Single `TeamMemoryService` class; separate `ConsolidationEngine` (Phase 2)
- **D-016**: `TeamMemoryService.promote()` — agents read freely, write through gate
- **D-017**: `_index.json` manifest for O(1) wiki-link resolution
- **D-018**: Grep + adaptive BM25-scored wiki-link traversal (max 3 hops)
- **D-019**: BM25 (Okapi BM25) via `rank-bm25` library
- **D-026**: Classification access control — agent clearance checked on every read
- **D-029**: Standalone service + thin `TeamMemoryBridge` in arcagent (bridge is separate spec)
- **D-030**: `fcntl.flock` per entity file; consolidation uses global lock
- **D-033**: Null Object pattern when `enabled = false`

## Traceability

| PRD Requirement | SDD Component | PLAN Phase |
|-----------------|---------------|------------|
| EG-* (Entity Graph) | MemoryStorage, EntityMetadata | Phase 1A-1B |
| IX-* (Index) | IndexManager | Phase 1B |
| SR-* (Search/Retrieval) | SearchEngine (BM25 + traversal) | Phase 1C |
| PG-* (Promotion Gate) | PromotionGate | Phase 1D |
| CL-* (Classification) | ClassificationChecker | Phase 1D |
| SV-* (Service Facade) | TeamMemoryService | Phase 1E |
| CLI-* (CLI) | cli.py | Phase 1F |
| TL-* (Telemetry) | AuditLogger + OTEL | Phase 1F |
| DC-* (Decisions Storage) | Existing StorageBackend | Phase 1A |

## Learnings

(Updated during implementation)

## Solutions Referenced

- `.claude/solutions/` (searched — no directly relevant prior solutions)
