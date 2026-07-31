# PRD: Bio-Memory v2.1 Completion (BIO-002)

## Overview

Complete the bio_memory module to cover all v2.1 spec requirements. BIO-001 delivered the foundation (working memory, identity, basic retrieval, basic light consolidation). This PRD covers the remaining four gaps needed for a fully functional memory system.

## Problem Statement

The current bio_memory module:
1. **Cannot search entity files** — Retriever only indexes `memory/` (episodes, working.md, how-i-work.md), missing `workspace/entities/`
2. **Has no team memory integration** — arcteam has a full TeamMemoryService (promotion gate, BM25 search, index) but bio_memory doesn't use it
3. **Light consolidation is incomplete** — creates episodes and updates identity, but does NOT update entity files, detect corrections, add co-occurrence links, or create new entity stubs
4. **Deep consolidation doesn't exist** — no entity rewrites, graph analysis, merge detection, or staleness management

## Functional Requirements

### Entity Search Scope (v2.1 RT-6, EG-1..6)

| ID | Requirement | v2.1 Ref |
|----|-------------|----------|
| ES-1 | Retriever searches `workspace/entities/**/*.md` when scope is None or "entities" | RT-6 |
| ES-2 | Wiki-link resolution checks `workspace/entities/` in addition to `memory/` | RT-2 |
| ES-3 | `memory_search` tool accepts `scope="entities"` | RT-6 |
| ES-4 | `memory_recall` resolves entity names against `workspace/entities/` | RT-6 |
| ES-5 | Context injection mentions entity file availability so LLM knows to search | RT-4 |
| ES-6 | Entity files with YAML frontmatter get frontmatter-boosted scoring | EG-1 |

### Team Memory Wiring (v2.1 PG-1..4)

| ID | Requirement | v2.1 Ref |
|----|-------------|----------|
| TW-1 | BioMemoryModule accepts optional `team_config` (convention-based from TeamSection) | PG-1 |
| TW-2 | Lazy-init TeamMemoryService — arcteam is an optional dependency | PG-1 |
| TW-3 | `memory_search` with scope="entities" also searches team entities (if team configured) | PG-1 |
| TW-4 | Team entity results scored with slight penalty vs agent-local entities | PG-1 |
| TW-5 | New entity stubs created via team promotion gate when team is configured | PG-2, PG-3 |
| TW-6 | All team promotions emit telemetry | PG-4 |

### Light Consolidation Completion (v2.1 LC-4..7, EL-1..3)

| ID | Requirement | v2.1 Ref |
|----|-------------|----------|
| LC-4 | For touched entities: update `last_verified` in YAML frontmatter, append to "Recent Activity" section | LC-4, EL-2 |
| LC-5 | If correction detected: LLM updates the relevant entity file's "Constraints and Lessons" section | LC-5, EL-3 |
| LC-6 | Co-occurrence linking: entities in same session but not linked get bidirectional `[[wiki-links]]` added to `links_to` frontmatter | LC-6 |
| LC-7 | New entities not yet in graph: create stub files with v2.1 schema. If team configured, promote via gate. | LC-7, EL-1 |
| LC-8 | Single LLM call analyzes session for all entity operations (touched, corrections, new, co-occurrences) | LC-9 (performance) |
| LC-9 | Entity normalization: legacy LLM-created files without frontmatter get v2.1 frontmatter added on first touch | EG-1 |

### Deep Consolidation Engine (v2.1 DC-1..15)

| ID | Requirement | v2.1 Ref |
|----|-------------|----------|
| DC-1 | Runs via manual trigger (tool or CLI). Optionally on schedule. | DC-1 |
| DC-2 | Activity check: count recent episodes. Zero = skip. Few = light pass. Many = full pass. | DC-2 |
| DC-3 | **Entity-centric pass**: For each entity touched by recent episodes: LLM reads entity + referencing episodes, rewrites file. Handles learning, forgetting, merging, compression. | DC-3 |
| DC-4 | **Episode-mediated link discovery**: During entity rewrite, LLM adds bidirectional `[[wiki-links]]` for entities mentioned across episodes. | DC-4 |
| DC-5 | **Domain cluster selection**: Select cluster by tag overlap or link neighborhood. | DC-5 |
| DC-6 | **Structural pattern linking**: Feed LLM frontmatter + summary of cluster entities. Discover non-obvious connections. Add bidirectional links. | DC-6 |
| DC-7 | **Domain rotation**: Rotate clusters across cycles. Track last scanned domain in state file. Busy domains scanned more often. | DC-7 |
| DC-8 | **Budget awareness**: Graph pass reads summaries (~100 tokens/entity), not full files. | DC-8 |
| DC-9 | **Merge detection**: Entity pairs with 3+ shared links -> LLM judges "same entity?" -> merge files, redirect links. | DC-9 |
| DC-10 | **Staleness**: Entities past TTL with no recent access -> flag as stale. Extended no-access -> archive. | DC-10, EL-5 |
| DC-11 | **how-i-work.md refresh**: LLM reads identity + recent episodes -> synthesizes patterns -> rewrite within budget. | DC-12 |
| DC-12 | **Index rebuild**: If team service available, trigger `rebuild_index()`. | DC-13 |
| DC-13 | **Idempotent and crash-safe**: Partial completion must not corrupt state. | DC-14 |
| DC-14 | **Telemetry**: Emit detailed audit events for each operation. | DC-15 |
| DC-15 | **Per-entity token budget enforcement**: Entity files exceeding budget get compressed during rewrite. | EG-6 |

### Research-Derived Requirements (from /deepen + decisions log)

| ID | Requirement | Source |
|----|-------------|--------|
| RD-1 | **Content-hash gating**: Skip entity rewrite if input (entity + episodes) SHA-256 matches last run. Expected 80-90% cost reduction. | SimpleMem, Bio-Memory /deepen |
| RD-2 | **Write-ahead manifest**: Deep consolidation writes `pending_entities.json` before processing. Each entity removed on success. Enables crash recovery. | Zep/Graphiti, existing FileBackend pattern |
| RD-3 | **5-step LLM output validation**: Before writing rewritten entities: (1) parse markdown, (2) check word count vs budget, (3) verify no frontmatter in output, (4) validate wiki-links against existing files, (5) retry once on failure. | LLM consolidation research |
| RD-4 | **Entity rewrite safety prompt**: Include "for each fact you drop, explicitly state why" to prevent silent information loss. | Bio-Memory /deepen entity rewrite pattern |
| RD-5 | **Bidirectional links from index**: `linked_from` is NOT stored in entity files — computed from `_index.json` at read-time. Eliminates cross-file write consistency problems. | D-021, wiki-link research |
| RD-6 | **Entity registry defense**: Only follow wiki-links that resolve to existing files. Rate-limit entity creation (max 3/session) and link creation (max 10/session). | Security research, dangling link injection |
| RD-7 | **Two-gate significance**: Deterministic pre-filter (message count, signal words) before LLM judgment. Reduces unnecessary LLM calls. | SimpleMem entropy research |
| RD-8 | **Sequential entity processing**: Process entities sequentially in deep consolidation to avoid coordination complexity when entities reference each other. | LLM consolidation research |
| RD-9 | **Entity prioritization**: Process entities by (1) oldest `last_updated`, (2) most pending episodes, (3) highest link count. | Decisions log D-035 |

### Configuration

| ID | Requirement |
|----|-------------|
| CF-1 | `entities_dirname: str = "entities"` — workspace-level entities directory |
| CF-2 | `per_entity_budget: int = 800` — token limit per entity file |
| CF-3 | `deep_consolidation_max_entities: int = 50` — max entities per deep cycle |
| CF-4 | `deep_consolidation_cluster_size: int = 20` — max entities per graph pass cluster |
| CF-5 | `staleness_ttl_days: int = 90` — days before entity flagged stale |
| CF-6 | `archive_dirname: str = "archive"` — directory for archived entities |
| CF-7 | `domain_rotation_state_file: str = ".consolidation-state.json"` — tracks rotation position |

## Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NF-1 | Light consolidation completes in < 30 seconds (v2.1 LC-9) |
| NF-2 | Deep consolidation processes up to 50 entities per cycle |
| NF-3 | All entity writes use `atomic_write_text` for crash safety |
| NF-4 | All LLM outputs sanitized before disk write (LLM05, ASI-06) |
| NF-5 | All entity operations emit OpenTelemetry audit events |
| NF-6 | arcteam is optional — ImportError handled gracefully |
| NF-7 | Zero new external dependencies |

## Out of Scope

- **Automatic retrieval (Gap 5)** — user wants LLM-driven retrieval via `memory_search` tool calls
- **Vector search fallback (RT-3)** — deferred to future spec
- **NATS memory events** — deferred to future spec
- **Convergence detection (DC-11)** — requires multi-agent setup, deferred
- **Classification-based access control** — handled by arcteam already
- **Encryption at rest** — deferred to hardening phase

## Security Considerations

| Threat | Mitigation |
|--------|------------|
| Entity file path traversal | All paths resolved + validated within workspace bounds |
| LLM output injection into entity files | `sanitize_text()` + `sanitize_wiki_link()` on all writes |
| Memory poisoning via entity frontmatter | YAML parsed with `safe_load`, never `load` |
| Prompt injection via conversation data | Boundary markers with session UUID on all LLM calls |
| arcteam dependency confusion | Lazy import with ImportError catch, no startup failure |
