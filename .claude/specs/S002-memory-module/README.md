# S002: Memory Module (Markdown Memory)

## Metadata

| Field | Value |
|-------|-------|
| **Spec ID** | S002 |
| **Feature** | Memory Module — 3-tier persistent memory with identity self-editing, ACE policy self-learning, and hybrid search |
| **Type** | Integration (module + core changes) |
| **Status** | PENDING |
| **Created** | 2026-02-15 |
| **Author** | Claude (spec-driven workflow) |
| **Confidence** | 90% (fast-track, brainstorm + build + deepen) |
| **Prior Work** | `.claude/brainstorms/2026-02-14-memory-module.md`, `.claude/decisions-log.md` (19 decisions, deepened) |

## Scope

A single Module Bus module (`memory.py`) providing 3-tier persistent memory for ArcAgent, plus core changes to enable multi-turn conversation:

1. **In-Context Memory** — identity.md (self-editable + audit), policy.md (ACE self-learning), context.md (agent-curated, ~2K budget)
2. **Daily Notes** — OpenClaw-pattern append-only daily logs with auto-injection
3. **Entity Storage** — Martian-pattern async entity extraction with JSONL fact logs
4. **Hybrid Search** — BM25 + sqlite-vec vector search across all memory tiers
5. **Session Management** — New `SessionManager` in core, JSONL transcripts, Letta-style compaction
6. **ACE Policy Engine** — Stanford ACE framework (arXiv:2510.04618) for self-improving agent policies

### Pre-requisites (outside memory module)

- **ArcRun**: Add `messages` parameter to `run()` for conversation persistence
- **ArcAgent core**: Add `SessionManager` class and `chat()` method on `ArcAgent`
- **ContextManager**: Add `agent:assemble_prompt` event emission

## NOT in Scope

- NATS inter-agent memory sharing (Phase 3)
- Module signing/verification (Phase 2)
- Neo4j/Graphiti graph storage backend (future plugin)
- Skill loading module (separate spec)
- Channel modules (separate spec)
- External evaluators / arcTeam (separate project)

## Key Decisions

All 19 decisions from build session (2026-02-14), enriched by deepening (2026-02-15):

| # | Decision | Rationale |
|---|----------|-----------|
| D-001 | Single Module Bus module with internal helper classes, Protocol-based | Simple registration, clean internal separation, plugin-ready |
| D-002 | Convention-based path matching for hook routing | Convention over configuration, matches workspace structure |
| D-003 | Auto-truncate context.md with summarization at ~2K token budget | Transparent to agent, module handles overflow |
| D-004 | Today + yesterday notes prepended to context section | Notes are "what's going on" context, seen before working memory |
| D-005 | Configurable eval model (cheap/fast) for policy evaluation | Cost savings, doesn't need primary model capability |
| D-006 | Entity extraction shares eval model config | One knob, both are background tasks |
| D-007 | Rich entity index (name, type, aliases, last_updated) | Efficient search without scanning directories |
| D-008 | Identity audit via telemetry events (+ JSONL defense-in-depth) | Consistent with existing audit, federal compliance backup |
| D-009 | Search DB at workspace/search.db (SQLite + sqlite-vec) | Portable, rebuildable, per-agent |
| D-010 | Local all-MiniLM-L6-v2 embeddings (384-dim) | No network dependency, air-gapped compatible |
| D-011 | Context Manager integration via agent:assemble_prompt event | Uses existing Module Bus, clean separation |
| D-012 | ArcRun stays stateless, messages parameter is pass-through | Clean boundary: ArcRun executes, ArcAgent remembers |
| D-013 | New SessionManager in core alongside ContextManager | Proven pattern, clean separation of concerns |
| D-014 | Append-only JSONL session transcripts with Letta-style compaction | Immutable audit trail, 30/70 sliding window |
| D-015 | Separate [eval] config section with full provider/model/temperature | Full control, independent of conversation model |
| D-016 | Hard veto on notes overwrite/delete via Module Bus pre_tool | Append-only means append-only, clear error |
| D-017 | Fire-and-forget entity extraction with task reference set | Never blocks user, follows existing bridge pattern |
| D-018 | Lazy search reindexing on memory_search call | Saves work when search isn't used |
| D-019 | Record/replay testing for LLM-dependent components | Realistic assertions, deterministic CI |

## Gap Analysis Fixes (post-OpenClaw/NanoClaw comparison)

| Gap | Resolution | Impact |
|-----|-----------|--------|
| Pre-compaction flush | Added `_pre_compact_flush` to SessionManager — extracts key facts from messages before compaction, appends to context.md (OpenClaw pattern) | REQ-SES-05-A, SDD SessionManager |
| Entity search via tool calling | No separate entity_search tool. Added `scope` and `date_from`/`date_to` params to memory_search so LLM directs where to look | REQ-SRC-03-A, REQ-SRC-03-B, SDD HybridSearch |
| Policy scoring granularity | Changed from helpful/harmful counters to 1-10 score with thresholds (<=2 remove, 3-4 at-risk, 5-7 improve, 8-10 promote). Asymmetric: +1 positive, -2 negative. Source tracking via session_id | REQ-POL-03 through REQ-POL-11, SDD PolicyEngine |
| Agent bootstrap | Added to roadmap as future brainstorm topic — not in S002 scope | roadmap.md |

## Steering Doc Corrections

| Steering Doc Says | Spec Corrects To | Reason |
|-------------------|------------------|--------|
| identity.md: "read-only to agent (admin-controlled)" | identity.md: self-editable with audit trail | MemGPT-inspired, enables persona evolution (D-008) |
| Policy: scored bullet system | Policy: ACE framework (Generator/Reflector/Curator) with 1-10 scoring | Stanford ACE paper + granular scoring for threshold-based lifecycle |
| Roadmap: Policy self-learning in Phase 4 | Policy self-learning included in memory module | User decision: build together, can revise later |

## Learnings

_(Updated during implementation)_

## Files

- [PRD.md](./PRD.md) — Product Requirements Document
- [SDD.md](./SDD.md) — System Design Document
- [PLAN.md](./PLAN.md) — Implementation Plan
