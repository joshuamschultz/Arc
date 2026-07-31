# BIO-001: Bio-Memory Module

| Field | Value |
|-------|-------|
| **ID** | BIO-001 |
| **Feature** | Biologically-inspired memory for ArcAgent |
| **Type** | Integration (module) |
| **Status** | PENDING |
| **Created** | 2026-02-21 |
| **Priority** | High |
| **Build Decisions** | `.claude/decisions-log.md` (Bio-Memory section, 24 decisions) |
| **Research** | `/deepen` completed (3 agents, 50+ sources) |
| **Design Doc** | `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` |

## Summary

Replaces the existing `MarkdownMemoryModule` as the default memory module. Implements biologically-inspired memory: working memory (scratchpad), identity (how-i-work.md), episodes (significant moments), retrieval (grep + wiki-link graph traversal), and consolidation (LLM-driven knowledge integration). Existing markdown-memory becomes opt-in alternative.

## Key Decisions

- **A-001**: Bio-memory is default; markdown-memory kept as alternative
- **A-002**: Mutually exclusive via config (`ConfigError` if both enabled)
- **A-005**: Facade (`BioMemoryModule`) + 5 helpers: `WorkingMemory`, `IdentityManager`, `EpisodeStore`, `Retriever`, `Consolidator`
- **P-003**: Grep-based retrieval with wiki-link following (one hop)
- **T-001**: Four tools: `memory_search`, `memory_note`, `memory_recall`, `memory_reflect`
- **I-002**: Light consolidation on `agent:shutdown` via `spawn_background`
- **S-001**: Sanitize on write (NFKC, strip zero-width/control chars, length limits)

## Traceability

| PRD Requirement | SDD Component | PLAN Phase |
|-----------------|---------------|------------|
| WM-* | WorkingMemory | Phase 1 |
| ID-* | IdentityManager | Phase 1 |
| EP-* | EpisodeStore | Phase 2 |
| RT-* | Retriever | Phase 1 |
| LC-* | Consolidator (light) | Phase 1 |
| DC-* | Consolidator (deep) | Phase 2 |
| T-* | Tool registration | Phase 1 |
| SEC-* | Sanitizer (shared util) | Phase 1 |

## Learnings

(Updated during implementation)

## Solutions Referenced

- `.claude/solutions/security-issues/` (if relevant patterns found)
