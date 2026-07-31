# PRD: ArcTeam Memory (SPEC-008)

**Version**: 1.0
**Date**: 2026-02-21
**Source**: ARC-Memory-System-v2.1-Final.md (Section 7) + Build Decisions (31) + Research Insights (6 areas)

---

## 1. Overview

ArcTeam Memory is the shared knowledge graph for multi-agent teams. It is the neocortical layer — slow to update, stable, shared, authoritative. Wiki-linked markdown entity files with YAML frontmatter, searchable via BM25 with adaptive graph traversal.

**Design Philosophy** (from design doc):
1. Entities are the atoms — wiki-linked markdown files are the knowledge graph
2. The LLM is the intelligence layer (consolidation rewrites, not algorithmic logic)
3. Token budgets are the forcing function (per-entity 800 token limit)
4. Agents read freely, write through promotion gate only

**Package boundary**: `TeamMemoryService` lives in `arcteam`. It is fully standalone — any framework (arcagent, langchain, crewai) calls it directly. Zero knowledge of arcagent internals.

## 2. Components

| Component | Storage | Lifecycle |
|-----------|---------|-----------|
| `entities/` | Wiki-linked `.md` files via `MemoryStorage` | Created through promotion gate or consolidation |
| `entities/{type}/` | Subdirectories by entity_type | Same as entities |
| `playbooks/` | Entity files with `entity_type=playbook` | Same lifecycle as entities |
| `decisions/` | Append-only JSONL via `StorageBackend` | Append on promote, never edited |
| `_index.json` | Derived manifest | Rebuilt on dirty flag |

## 3. Functional Requirements

### 3.1 Entity Graph (EG)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| EG-1 | Entities are markdown files with YAML frontmatter | D-012 |
| EG-2 | Wiki-links `[[entity-id]]` create connections | Design doc EG-2 |
| EG-3 | Domain entities hold cross-cutting behavioral knowledge | Design doc EG-3 |
| EG-4 | entity_type maps to subdirectory (person, organization, project, domain, process) | D-022 |
| EG-5 | Custom entity types configurable via TOML | D-022 |
| EG-6 | Per-entity file budget: 800 tokens (configurable) | D-032 |
| EG-7 | Playbooks stored as entities (`entity_type=playbook`), searchable, consolidatable | D-023 |
| EG-8 | New entities created only through promotion gate, consolidation, or human edits | Design doc EG-5 |
| EG-9 | Entity frontmatter: entity_type, entity_id, name, status, last_updated, last_verified, created, links_to, linked_from (derived), tags, source_agents, classification | Design doc |
| EG-10 | Entity body sections: Summary, Key Facts, Constraints and Lessons, Recent Activity | Design doc |

### 3.2 Index Management (IX)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| IX-1 | `_index.json` provides fast entity lookup manifest (derived, rebuildable) | D-017 |
| IX-2 | Rich schema: id, path, type, tags, links_to, linked_from, summary snippet, last_updated, status | D-021 |
| IX-3 | O(1) entity_id to path resolution via index | D-017 |
| IX-4 | Lazy rebuild with dirty flag (`.dirty` marker file) | D-031 |
| IX-5 | Writes touch dirty flag; next read rebuilds if dirty | D-031 |
| IX-6 | Index rebuild: read frontmatter only (not full body), write atomically | Research |
| IX-7 | Federal tier: integrity checksum on index load | D-017 |

### 3.3 Search and Retrieval (SR)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| SR-1 | Grep-based search with adaptive BM25-scored wiki-link traversal | D-018 |
| SR-2 | BM25 (Okapi BM25) via `rank-bm25` library for hop relevance scoring | D-019 |
| SR-3 | Max hops configurable (default 3), traversal stops if BM25 score drops below threshold | D-018 |
| SR-4 | Threshold: `0.3 * max_initial_score` (adaptive to query) | Research |
| SR-5 | BFS traversal — closest neighbors first | Research |
| SR-6 | Cycle detection via `visited: set[str]` | Research |
| SR-7 | Search results: `list[SearchResult]` Pydantic models with entity_id, path, snippet, score, hops, entity_type, tags | D-024 |
| SR-8 | Federal tier: `classification` field on SearchResult | D-024 |
| SR-9 | Tokenized corpus cached with dirty-flag-gated invalidation (rebuild per search is <50ms for <1000 files) | Research |
| SR-10 | Strip YAML frontmatter, markdown syntax, code blocks before BM25 indexing | Research |
| SR-11 | Bidirectional traversal — forward links (links_to) and backlinks (linked_from from index) | Research |
| SR-12 | Backlinks computed from `_index.json`, not maintained in individual files | Research |

### 3.4 Promotion Gate (PG)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| PG-1 | `TeamMemoryService.promote()` is the single write entry point | D-016 |
| PG-2 | Schema validation via Pydantic (EntityMetadata) on all promotions | D-027 |
| PG-3 | Classification label required on all entities | D-003, D-027 |
| PG-4 | UNCLASSIFIED: auto-approved, written immediately | D-027 |
| PG-5 | CUI+: queued for human approval via `memory-approval` messaging channel | D-027, D-028 |
| PG-6 | All promotions audit-logged regardless of outcome | D-027 |
| PG-7 | Grep check before creating — update existing entity if found | Design doc EL-1 |
| PG-8 | Entity file written via atomic write (tempfile + os.replace) | D-012, Research |
| PG-9 | Dirty flag touched after write | D-031 |
| PG-10 | Federal tier: blocks without classification label. Enterprise: warns. Personal: no enforcement. | D-016 |

### 3.5 Classification Access Control (CL)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| CL-1 | Agent has `max_classification` in config | D-026 |
| CL-2 | Every read/search checks entity classification against agent clearance | D-026 |
| CL-3 | Entities above clearance are invisible (silent filter) | D-026, Research |
| CL-4 | Do not inform agent that filtered results exist (information leakage prevention) | Research |
| CL-5 | Denied access audit-logged (NIST 800-53 AU-2) | D-026, Research |
| CL-6 | Classification hierarchy: UNCLASSIFIED < CUI < CONFIDENTIAL < SECRET < TOP_SECRET | Research |
| CL-7 | Graph traversal pruned at classification boundary — branch stops if target entity exceeds clearance | Research |
| CL-8 | Classification can only be lowered by human (never raised automatically, never changed by consolidation) | Research |
| CL-9 | Federal: hard block. Enterprise: warn + block. Personal: no enforcement. | D-026 |

### 3.6 Decisions Storage (DS)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| DS-1 | Decisions are append-only JSONL via existing `StorageBackend` | D-023 |
| DS-2 | Created from promoted episodes | D-023 |
| DS-3 | Federal tier: chained HMAC on decisions JSONL | D-023 |
| DS-4 | Searchable via grep on JSONL content | — |

### 3.7 Telemetry and Audit (TL)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| TL-1 | Audit-only for Phase 1 — no messaging events | D-020 |
| TL-2 | AuditLogger for compliance (every read/write/search) | D-025 |
| TL-3 | Structured logging for operations | D-025 |
| TL-4 | OpenTelemetry spans for distributed tracing | D-025 |
| TL-5 | Federal: all required, 100% sampling. Enterprise: configurable. Personal: opt-in. | D-025 |
| TL-6 | Every search query + results returned audit-logged (federal) | D-018 |

### 3.8 Concurrency (CC)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| CC-1 | Reads are lock-free | D-030 |
| CC-2 | Writes use `fcntl.flock` per entity file | D-030 |
| CC-3 | Consolidation uses global `.consolidation.lock` | D-030 |
| CC-4 | Lock pattern matches existing `FileBackend` (storage.py) | D-030 |
| CC-5 | `asyncio.to_thread` for all file I/O | Research |
| CC-6 | Lock timeout: 5 retries with backoff (~1.5s max) | Research |

### 3.9 Service Facade (SV)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| SV-1 | Single `TeamMemoryService` class — discoverable API | D-013 |
| SV-2 | Methods: `search()`, `promote()`, `get_entity()`, `list_entities()` | D-013 |
| SV-3 | Null Object pattern when `enabled = false` — empty results, no-op writes | D-033 |
| SV-4 | Zero knowledge of arcagent — standalone service | D-029 |
| SV-5 | Consolidation trigger: first session checks `.last_consolidated` timestamp | D-014 |
| SV-6 | Federal: consolidation mandatory (blocks until done). Enterprise: async. Personal: optional. | D-014 |

### 3.10 CLI Commands (CLI)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| CLI-1 | `arc team-memory status` — entity count, index status, last consolidated | — |
| CLI-2 | `arc team-memory search "query"` — BM25 search with results | — |
| CLI-3 | `arc team-memory entity show <id>` — display entity file | — |
| CLI-4 | `arc team-memory entity list` — list all entities | — |
| CLI-5 | `arc team-memory index rebuild` — force index rebuild | — |
| CLI-6 | `arc team-memory promote <file>` — promote a file to team memory | — |

## 4. Configuration

```toml
[team_memory]
enabled = true
path = ".arc/team/"

[team_memory.entities]
path = ".arc/team/entities/"
index = ".arc/team/entities/_index.json"
entity_types = ["person", "organization", "project", "domain", "process", "playbook"]
per_entity_budget = 800  # tokens

[team_memory.search]
max_hops = 3
bm25_threshold_ratio = 0.3
max_results = 20

[team_memory.consolidation]
enabled = true
schedule = "daily_2am"
adaptive_intensity = true
max_entities_per_cycle = 50
model = ""  # empty = arcllm default

[team_memory.security]
read_access = "all_agents"
write_access = "promotion_gate_only"
classification_required = true
encryption_at_rest = false  # enable for CUI environments
```

## 5. Entity File Schema

```markdown
---
entity_type: agency
entity_id: nnsa
name: NNSA
status: active
last_updated: 2026-02-21
last_verified: 2026-02-21
created: 2025-09-15
links_to: ["DOE", "Genesis Mission", "sarah-chen", "procurement"]
tags: [federal, energy, nuclear]
source_agents: [procurement-agent, strategy-agent]
classification: unclassified
---

# NNSA

## Summary
[LLM-maintained. Compressed to fit per-file budget.]

## Key Facts
[Verifiable, current facts. Updated during consolidation.]

## Constraints and Lessons
[Behavioral knowledge specific to this entity.]

## Recent Activity
[Append during light consolidation. Absorbed into Summary during deep consolidation.]
```

## 6. Tier Variations

| Component | Federal | Enterprise | Personal |
|-----------|---------|------------|----------|
| Audit events | Required (block without) | Default on | Off (opt-in) |
| Classification enforcement | Hard block | Warn + block | No enforcement |
| Encryption at rest | AES-256 mandatory | Default on | Off |
| Promotion validation | Schema + classification required | Schema required | Schema only |
| CUI+ approval | Human approval required | Configurable | Auto-approved |
| Search audit | Every query logged | Configurable | Off |
| Consolidation | Mandatory, blocks session | Async | Optional |
| Index integrity | Checksum verified | No checksum | No checksum |
| Decisions HMAC | Chained HMAC | No chain | No chain |

## 7. Build Phases

| Phase | Scope |
|-------|-------|
| Phase 1 — Core Service | MemoryStorage, entity model, _index.json, BM25 search, wiki-link traversal, promotion gate, classification, TeamMemoryService facade, CLI, telemetry |
| Phase 2 — Consolidation | ConsolidationEngine (entity + graph pass), merge detection, staleness/archival, convergence |
| Phase 3 — Scale | Vector search fallback, NATS events, TeamMemoryBridge (in arcagent) |
| Phase 4 — Harden | Encryption at rest, FedRAMP audit, SBOM, provenance |

**This spec covers Phase 1 only.**

## 8. Non-Goals (This Phase)

- ConsolidationEngine (Phase 2)
- Deep consolidation passes (Phase 2)
- Merge detection / archival (Phase 2)
- Vector search fallback (Phase 3)
- NATS memory events (Phase 3)
- TeamMemoryBridge in arcagent (Phase 3 — separate spec)
- Encryption at rest (Phase 4)
- SBOM / provenance (Phase 4)

## 9. Dependencies

### New Dependencies

| Library | Purpose | Size |
|---------|---------|------|
| `rank-bm25` | BM25 scoring for search | Pure Python, ~50 LOC internally |
| `python-frontmatter` | YAML frontmatter + markdown I/O | Pure Python, depends on PyYAML |

### Existing Dependencies (reused)

| Library | Purpose |
|---------|---------|
| `pydantic` | EntityMetadata, SearchResult, config validation |
| `opentelemetry` | Audit spans and metrics |
| `arcllm` | LLM calls for consolidation (Phase 2 — not needed Phase 1) |

### Sibling Package Dependencies

| Package | What | How |
|---------|------|-----|
| `arcteam.storage` | `StorageBackend` for decisions JSONL | Direct import (same package) |
| `arcteam.audit` | `AuditLogger` for compliance events | Direct import (same package) |
| `arcteam.messenger` | `MessagingService` for CUI+ approval queue | Direct import (same package) |

## 10. Success Criteria

- TeamMemoryService loads and functions as standalone service
- Entity files created/read with valid YAML frontmatter
- `_index.json` built and maintained via dirty flag
- BM25 search returns relevant results with wiki-link traversal
- Promotion gate validates schema, classification, audit-logs all operations
- Classification access control filters results silently
- Null Object pattern works when `enabled = false`
- CLI commands functional
- Audit trail on all memory operations
- Concurrency: flock-based writes, lock-free reads
- Zero knowledge of arcagent — fully standalone
- Tests: unit per component, integration through service facade
