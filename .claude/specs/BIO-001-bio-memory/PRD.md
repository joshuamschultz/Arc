# PRD: Bio-Memory Module (BIO-001)

**Version**: 1.0
**Date**: 2026-02-21
**Source**: ARC-Memory-System-v2.1-Final.md + Build Decisions (24) + Research Insights (3 agents)

---

## 1. Overview

Bio-memory is a biologically-inspired memory module for ArcAgent. It replaces the existing `MarkdownMemoryModule` as the default memory system while keeping it as an opt-in alternative.

**Design Philosophy** (from design doc):
1. Everything is a node in the graph (wiki-linked markdown files)
2. The LLM is the intelligence layer (no algorithmic decision logic)
3. Token budgets are the forcing function (budgets create pruning behavior)

## 2. Components

| Component | File | Lifecycle |
|-----------|------|-----------|
| Working Memory | `memory/working.md` | Overwritten every turn. Cleared at session end. |
| Identity | `memory/how-i-work.md` | Read at session start. Updated by LLM during consolidation. 500 token budget. |
| Episodes | `memory/episodes/*.md` | Created at session end if significant. Append-only. Never edited. |

## 3. Functional Requirements

### 3.1 Working Memory (WM)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| WM-1 | Single markdown file, overwritten every turn | D-001 |
| WM-2 | YAML frontmatter: topics, semantic tags, entity refs, importance, turn number, timestamp | D-001 |
| WM-3 | LLM-written markdown body (turn state) | D-001 |
| WM-4 | Flushed to long-term storage at session end before clearing | — |
| WM-5 | Starts empty on session start | — |
| WM-6 | Budget: 500 tokens. If exceeded, LLM summarizes. | P-001 |
| WM-7 | Never read by other agents | F-006 |

### 3.2 Identity (ID)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| ID-1 | Contains learned behavioral patterns, working style, cross-cutting lessons | — |
| ID-2 | NOT configured by humans. Written and maintained entirely by the LLM. | — |
| ID-3 | Read at session start and injected into context for the entire session | I-001 |
| ID-4 | Updated during light consolidation if session produced behavioral insights | I-002 |
| ID-5 | Updated during deep consolidation as LLM synthesizes cross-session patterns | I-003 |
| ID-6 | Budget: 500 tokens. If exceeded, LLM compresses. | P-001, D-003 |
| ID-7 | Starts empty for new agent. Grows organically. | — |
| ID-8 | Minimal frontmatter: last_updated, token_count, version | D-003 |

### 3.3 Episodes (EP)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| EP-1 | Created at session end if LLM judges session was significant | I-002 |
| EP-2 | Significance: decisions made, corrections received, novel information, emotional weight | — |
| EP-3 | Rich frontmatter: date, type, significance, participants, emotional_signal, entities_touched, source_agent, tags, links_to | D-002 |
| EP-4 | Body: LLM narrative of what happened, decisions made, why it matters | D-002 |
| EP-5 | Links to entity files via `[[wiki-links]]` | D-002 |
| EP-6 | Append-only. Never edited after creation. | S-002 |
| EP-7 | Naming: `YYYY-MM-DD-{llm-slug}.md` | D-004 |
| EP-8 | Personal — not readable by other agents | F-006 |

### 3.4 Retrieval (RT)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| RT-1 | Agent uses memory tools for on-demand retrieval (no automatic retrieval decisions) | I-001 |
| RT-2 | Primary: grep-based with wiki-link following (one hop) | P-003 |
| RT-3 | Search scope: episodes, how-i-work.md, working.md | — |
| RT-4 | Retrieved content wrapped in boundary markers (prompt injection defense) | S-001 |
| RT-5 | Budget: 3,000 tokens. Overflow strategy configurable (truncate/summarize/skip_least_connected) | — |
| RT-6 | Emit telemetry: files retrieved, path used, tokens consumed | O-001 |
| RT-7 | Frontmatter-first two-pass search: grep frontmatter for tags/entities, then full-text on matched subset | Research |

### 3.5 Tools (T)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| T-1 | `memory_search` — search across memory tiers (grep + wiki-link traversal) | T-001 |
| T-2 | `memory_note` — create an episode or append to working memory | T-001 |
| T-3 | `memory_recall` — retrieve specific entity/episode by name | T-001 |
| T-4 | `memory_reflect` — trigger identity/consolidation reflection | T-001 |

### 3.6 Light Consolidation (LC)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| LC-1 | Triggers on `agent:shutdown` via `spawn_background` | I-002 |
| LC-2 | Non-blocking. Failure-tolerant. | I-002 |
| LC-3 | LLM evaluates session significance | — |
| LC-4 | If significant: create episode file | — |
| LC-5 | LLM evaluates: "Has anything about how this agent operates changed?" If yes, update how-i-work.md within 500-token budget | — |
| LC-6 | Clear working.md | — |
| LC-7 | Complete in < 30 seconds | — |

### 3.7 Deep Consolidation (DC)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| DC-1 | Triggered via scheduler module + CLI (no internal timer) | I-003 |
| DC-2 | Pass 1: Entity-centric rewrite (read entity + referencing episodes, rewrite) | — |
| DC-3 | Pass 2: Graph-centric structural linking (domain cluster analysis) | — |
| DC-4 | Identity refresh: synthesize cross-session patterns into how-i-work.md | — |
| DC-5 | Idempotent and crash-safe (content-hash gating + atomic writes) | Research |
| DC-6 | Emit detailed telemetry | O-001 |

### 3.8 Security (SEC)

| ID | Requirement | Decision Ref |
|----|-------------|-------------|
| SEC-1 | All memory writes emit audit events | F-001 |
| SEC-2 | Audit logs tamper-evident (append-only JSONL + OTel) | F-002 |
| SEC-3 | Memory content encrypted at rest (federal tier) | F-003 |
| SEC-4 | Memory content validated on read (integrity check) | F-004 |
| SEC-5 | PII/CUI filtered before storage (federal tier) | F-005 |
| SEC-6 | Per-agent memory isolation | F-006 |
| SEC-7 | Classification tracking in frontmatter (federal tier) | F-007 |
| SEC-8 | Sanitize on write: NFKC, strip zero-width/control chars, length limits | S-001 |
| SEC-9 | Bash veto for memory paths. Episodes append-only. | S-002 |
| SEC-10 | Sanitizer in `arcagent/utils/` (shared, not in either memory module) | S-002 |
| SEC-11 | Randomized boundary markers per-session (UUID-embedded tags) | Research |

## 4. Configuration

```toml
[modules.bio_memory]
enabled = true
config = {}

[modules.bio_memory.config]
# Token budgets
total_per_turn = 4000
identity_budget = 500
retrieved_budget = 3000
working_budget = 500
overflow_strategy = "truncate"  # "truncate" | "summarize" | "skip_least_connected"

# Consolidation
light_on_shutdown = true
significance_model = "llm"
```

**Mutual exclusivity**: `[modules.memory]` (markdown-memory) and `[modules.bio_memory]` cannot both be enabled. Raises `ConfigError` on validation. (Decision A-002)

## 5. Module Bus Events

| Event | Handler | Priority | Purpose |
|-------|---------|----------|---------|
| `agent:assemble_prompt` | Inject how-i-work.md + working.md | 50 | Identity context |
| `agent:post_respond` | Update working.md | 100 | Turn state |
| `agent:pre_tool` | Bash veto for memory paths | 10 | Security |
| `agent:post_tool` | Audit trail | 100 | Compliance |
| `agent:shutdown` | Light consolidation | 100 | End-of-session |

No new core events needed. (Decision A-008)

## 6. CLI Commands

```bash
arc memory status          # Budget usage, episode count, identity file size
arc memory identity show   # Show how-i-work.md
arc memory episodes list   # List episodes
arc memory working show    # Current scratchpad
arc memory search "query"  # Grep-based search
arc memory consolidate     # Trigger light consolidation
arc memory consolidate --deep    # Trigger deep consolidation
arc memory consolidate --dry-run # Preview changes
```

(Decision U-001)

## 7. Tier Variations

| Component | Federal | Enterprise | Personal |
|-----------|---------|------------|----------|
| Audit events | Required (block without) | Default on (warn if disabled) | Off (opt-in) |
| Encryption at rest | AES-256 mandatory | Default on | Off |
| PII filtering | Block storage | Warn | Skip |
| Classification frontmatter | Required | Optional | Skip |
| Memory isolation | Always on | Always on | Always on |
| Content sanitization | Always on | Always on | Always on |

## 8. Build Phases

| Phase | Version | Scope |
|-------|---------|-------|
| Phase 1 — Core | v0.1 | working.md, how-i-work.md, grep retrieval, token budgets, memory tools, light consolidation |
| Phase 2 — Intelligence | v0.2 | Episode recording, deep consolidation (entity + graph pass), promotion gate hooks |
| Phase 3 — Scale | v0.3 | Vector search fallback, NATS events, adaptive consolidation |
| Phase 4 — Harden | v0.4 | Classification access control, encryption at rest, provenance, FedRAMP audit |

**This spec covers Phase 1 only.** Phases 2-4 will be specified incrementally.

## 9. Non-Goals (This Phase)

- Deep consolidation (Phase 2)
- Episode recording (Phase 2)
- Vector search fallback (Phase 3)
- NATS memory events (Phase 3)
- Encryption at rest (Phase 4)
- Classification access control (Phase 4)
- Team memory integration (separate build)

## 10. Success Criteria

- Bio-memory module loads and functions as default memory
- Existing markdown-memory still works when configured
- Mutual exclusivity enforced
- Working.md lifecycle correct (overwrite/turn, clear/session-end)
- how-i-work.md injected into prompt context
- Grep-based retrieval with wiki-link following returns relevant results
- Four memory tools registered and functional
- Light consolidation runs on shutdown without blocking
- All memory writes emit audit events
- Content sanitization prevents injection vectors
- Token budgets enforced
- Tests: unit per helper class, integration through bus
