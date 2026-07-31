# PRD: Memory Module (Markdown Memory)

**Spec ID**: S002
**Status**: PENDING
**Last Updated**: 2026-02-15

---

## 1. Overview

### 1.1 Problem Statement

ArcAgent has a functioning nucleus (S001) but no memory persistence. The agent cannot maintain working memory between turns, remember user preferences, track entities, log daily notes, or learn from experience. Every `run()` call creates fresh messages, losing all prior context. Without memory, agents are limited to single-shot tasks.

### 1.2 Target Users

- **Agents** — Need persistent memory to be useful beyond single-shot tasks
- **Developers** — Building with ArcAgent, expect agents that remember
- **Federal users** — Need auditable memory (file-based, git-diffable, no opaque databases)

### 1.3 Success Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| Multi-turn conversation | Agent remembers prior turns | Integration test: chat() persists messages |
| Identity self-editing | Agent can update identity.md with audit trail | Unit test: write + audit event emitted |
| Policy self-learning | Agent evaluates and updates policy.md via ACE | Integration test: post-task evaluation adds bullet |
| Context.md budget | Overflow auto-truncated to ~2K tokens | Unit test: oversized write → truncation |
| Daily notes | Append-only, today+yesterday in prompt | Unit test: prepend to context section |
| Entity extraction | Async post-response extraction | Integration test: entity appears in facts.jsonl |
| Hybrid search | BM25 + vector results for memory_search | Integration test: returns ranked results |
| Session persistence | JSONL transcript saved, resumable | Unit test: save + load roundtrip |
| Session compaction | Sliding window at 85% threshold | Integration test: older messages summarized |

---

## 2. Requirements

### 2.1 Core Changes (Pre-requisites)

#### 2.1.1 SessionManager (session_manager.py — NEW core component)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-SES-01 | Create and manage conversation sessions with UUID4 session IDs | P0 | D-013 |
| REQ-SES-02 | Append messages to in-memory list with asyncio.Lock | P0 | D-013, Deepening |
| REQ-SES-03 | Persist messages as append-only JSONL at `workspace/sessions/{id}.jsonl` | P0 | D-014 |
| REQ-SES-04 | Load previous session from JSONL with malformed-line tolerance | P0 | D-014, Deepening |
| REQ-SES-05 | Trigger compaction at compact_threshold (85% of max_tokens) | P0 | D-014, Deepening |
| REQ-SES-05-A | Pre-compaction flush: extract key facts from messages-to-be-compacted and append to context.md before summarizing (OpenClaw pattern — never lose info) | P0 | Gap 1, OpenClaw |
| REQ-SES-06 | Sliding window compaction: summarize oldest 30%, preserve recent 70% | P0 | D-014, Letta research |
| REQ-SES-07 | Store compaction summaries as `type: "compaction_summary"` JSONL entries | P1 | Deepening |
| REQ-SES-08 | Configurable session retention (keep last N or last 30 days) | P2 | Deepening |

#### 2.1.2 ArcAgent Orchestrator Changes (agent.py — MODIFY)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-AGT-10 | Add `chat()` method for multi-turn conversation | P0 | Brainstorm |
| REQ-AGT-11 | Pass session messages to `arcrun.run()` via messages parameter | P0 | D-012 |
| REQ-AGT-12 | Initialize SessionManager in startup sequence | P0 | D-013 |

#### 2.1.3 ArcRun Changes (loop.py — MODIFY in sibling project)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-RUN-01 | Accept `messages` parameter in `run()` | P0 | D-012 |
| REQ-RUN-02 | Prepend system prompt to provided messages (always rebuild fresh) | P0 | D-012, Deepening |

#### 2.1.4 ContextManager Changes (context_manager.py — MODIFY)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-CTX-10 | Emit `agent:assemble_prompt` event during system prompt assembly | P0 | D-011 |
| REQ-CTX-11 | Accept injected content sections from event handlers | P0 | D-011, Deepening |
| REQ-CTX-12 | Return mutable PromptSections dict (not raw string) for injection | P1 | Deepening |

#### 2.1.5 Config Changes (config.py — MODIFY)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-CFG-10 | Add `[eval]` config section (provider, model, max_tokens, temperature, timeout, fallback_behavior) | P0 | D-015 |
| REQ-CFG-11 | Add `[memory]` config section (context_budget_tokens, search_weights, max_concurrent_evals) | P0 | Various decisions |
| REQ-CFG-12 | Add `[session]` config section (retention_count, retention_days) | P1 | D-014 |

### 2.2 Memory Module (arcagent/modules/memory/)

#### 2.2.1 Main Module (markdown_memory.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-MEM-01 | Implement `Module` protocol (name, startup, shutdown) | P0 | D-001 |
| REQ-MEM-02 | Subscribe to `agent:pre_tool` and `agent:post_tool` for hook routing | P0 | D-002 |
| REQ-MEM-03 | Route hooks by convention-based workspace-relative path matching | P0 | D-002 |
| REQ-MEM-04 | Canonicalize paths with `Path.resolve()` before matching | P0 | D-002, Deepening |
| REQ-MEM-05 | Guard against re-entrancy with `_hook_active` flag | P1 | D-002, Deepening |
| REQ-MEM-06 | Subscribe to `agent:assemble_prompt` to inject notes | P0 | D-004, D-011 |
| REQ-MEM-07 | Subscribe to `agent:post_respond` for async entity extraction | P0 | D-017 |
| REQ-MEM-08 | Register `memory_search` as a native tool | P0 | Brainstorm |
| REQ-MEM-09 | Intercept `bash` tool pre_tool events for memory path targeting | P1 | D-016, Deepening |
| REQ-MEM-10 | Track background tasks in `_background_tasks` set with done callbacks | P0 | D-017, Deepening |
| REQ-MEM-11 | Cancel active background tasks during shutdown | P0 | D-017, Deepening |

#### 2.2.2 Identity Self-Editing

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-IDT-01 | Allow agent writes to identity.md (no veto) | P0 | D-008, Brainstorm |
| REQ-IDT-02 | Emit audit event with before/after content, session ID, triggering context | P0 | D-008 |
| REQ-IDT-03 | Write append-only JSONL audit at `workspace/audit/identity-changes.jsonl` | P1 | D-008, Deepening (NIST defense-in-depth) |

#### 2.2.3 Policy Self-Learning (ACE Framework)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-POL-01 | Implement ACE Reflector: evaluate agent work after multi-step tasks, user feedback, every N turns (default 10) | P0 | ACE paper, Brainstorm |
| REQ-POL-02 | Implement ACE Curator: deterministic non-LLM merge of delta updates into policy.md | P0 | ACE paper |
| REQ-POL-03 | Structured bullet format: `- [PXX] [lesson text] {score:N, uses:N, reviewed:DATE, source:SESSION_ID}` | P0 | ACE paper + Gap 3 |
| REQ-POL-04 | Delta updates: append new bullets (score 5), adjust scores, rewrite text — never full rewrite | P0 | ACE paper |
| REQ-POL-05 | De-duplicate bullets via semantic similarity (cosine > 0.85 = duplicate) | P1 | ACE paper |
| REQ-POL-06 | Remove bullets with score <= 2 (auto-removal threshold) | P0 | ACE paper |
| REQ-POL-07 | Score thresholds: <=2 remove, 3-4 at-risk, 5-7 improve, 8-10 promote | P0 | User decision |
| REQ-POL-08-A | Score changes: +1 for positive, -2 for negative (penalize faster than reward) | P0 | User decision |
| REQ-POL-07-A | Decay: score -1 every 30 days of non-use | P2 | ACE paper |
| REQ-POL-11 | Source tracking: each bullet records session_id that created/last updated it | P0 | Gap 3 |
| REQ-POL-08 | Use eval model (D-005/D-015) for reflection, not primary model | P0 | D-005 |
| REQ-POL-09 | Semaphore limiting concurrent eval calls (default max 2) | P1 | D-015, Deepening |
| REQ-POL-10 | Graceful fallback: skip evaluation if eval model unavailable | P0 | D-015, Deepening |

#### 2.2.4 Context.md Management

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-WM-01 | Enforce ~2K token budget on context.md writes | P0 | D-003 |
| REQ-WM-02 | Auto-truncate oldest entries when budget exceeded | P0 | D-003 |
| REQ-WM-03 | Summarize truncated content via eval model (fallback: truncate without summary) | P1 | D-003, Deepening |

#### 2.2.5 Daily Notes

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-NOT-01 | Enforce append-only on `workspace/notes/*.md` via pre_tool veto | P0 | D-016 |
| REQ-NOT-02 | Block overwrites, deletions, and renames on notes files | P0 | D-016 |
| REQ-NOT-03 | Priority 10 (policy level) for append-only handler | P0 | D-016, Deepening |
| REQ-NOT-04 | Inject today + yesterday notes into system prompt (before context section) | P0 | D-004 |
| REQ-NOT-05 | Token budget on injected notes (~1K today + ~500 yesterday) | P1 | D-004, Deepening |
| REQ-NOT-06 | Graceful handling when notes files don't exist | P0 | D-004, Deepening |

#### 2.2.6 Entity Extraction

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-ENT-01 | Extract entities from most recent assistant+user exchange | P0 | D-006, Deepening |
| REQ-ENT-02 | Structured output schema: `{entities: [{name, type, aliases, facts: [{predicate, value, confidence}]}]}` | P0 | D-006, Deepening |
| REQ-ENT-03 | Check index.json for existing entity match (case-insensitive + alias) | P0 | D-007 |
| REQ-ENT-04 | New entity: create `entities/{name}/facts.jsonl` + `summary.md` + update index | P0 | D-007 |
| REQ-ENT-05 | Existing entity: append fact to JSONL, detect contradictions (supersede, don't delete) | P0 | D-007 |
| REQ-ENT-06 | Atomic write for index.json (write-to-temp + rename) | P0 | D-007, Deepening |
| REQ-ENT-07 | asyncio.Lock on index writes | P0 | D-007, Deepening |
| REQ-ENT-08 | Skip extraction for trivial exchanges ("ok", "thanks") | P1 | D-006, Deepening |
| REQ-ENT-09 | Semaphore limiting concurrent extractions (default max 2) | P1 | D-017, Deepening |

#### 2.2.7 Hybrid Search

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-SRC-01 | BM25 keyword search across notes, entities, context | P0 | Brainstorm |
| REQ-SRC-02 | Vector similarity search via sqlite-vec (384-dim float32) | P0 | D-009, D-010 |
| REQ-SRC-03 | Configurable weights (default 70% BM25 / 30% vector) | P0 | Brainstorm |
| REQ-SRC-03-A | Accept `scope` parameter (notes/entities/context/sessions/None) to let LLM direct where to search | P0 | Gap 2 |
| REQ-SRC-03-B | Accept `date_from` and `date_to` parameters for temporal filtering | P1 | Gap 2 |
| REQ-SRC-04 | Lazy reindexing: only rebuild when source files changed since last index | P0 | D-018 |
| REQ-SRC-05 | File modification timestamps for change detection | P0 | D-018, Deepening |
| REQ-SRC-06 | Chunk documents at ~400 tokens, heading-boundary preferred | P0 | D-018, Deepening |
| REQ-SRC-07 | Lazy embedding model loading (first search only) | P0 | D-010, Deepening |
| REQ-SRC-08 | Graceful fallback to BM25-only if sqlite-vec unavailable | P1 | D-009, Deepening |
| REQ-SRC-09 | Store model name in search.db metadata for embedding version tracking | P1 | D-010, Deepening |
| REQ-SRC-10 | `rebuild_search_db()` command for manual reindex | P1 | D-009, Deepening |
| REQ-SRC-11 | WAL mode for concurrent reads during reindex | P0 | D-018, Deepening |
| REQ-SRC-12 | Fast path: skip vector search if BM25 returns high-confidence results | P2 | D-018, Deepening |

---

## 3. Non-Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-10 | Memory module LOC | < 1,500 lines across all module files |
| NFR-11 | Core changes LOC | < 500 lines added to existing core |
| NFR-12 | Core total LOC | Remains < 3,000 (currently ~1,500) |
| NFR-13 | Entity extraction latency | Does not block user response |
| NFR-14 | Search cold start | < 5 seconds (including embedding model load) |
| NFR-15 | Type safety | `mypy --strict` passes on all new code |
| NFR-16 | Async-first | All I/O uses asyncio |
| NFR-17 | Embedding model size | < 100MB (all-MiniLM-L6-v2 = ~43MB, or ONNX variant) |
| NFR-18 | Python 3.12+ | Minimum supported version |

---

## 4. Acceptance Criteria

### 4.1 Integration Test: Multi-Turn Conversation

```
Given: Agent with memory module loaded, valid config
When: User sends "my name is Josh", agent responds, user sends "what is my name?"
Then:
  - Agent remembers "Josh" from prior turn
  - Session messages persisted to JSONL
  - Entity extracted asynchronously (entities/josh/facts.jsonl created)
  - Daily note appended with conversation summary
```

### 4.2 Integration Test: Policy Self-Learning (ACE)

```
Given: Agent completes a multi-step task
When: Post-task evaluation triggers
Then:
  - Reflector (eval model) produces delta with lessons learned
  - Curator merges deltas into policy.md deterministically
  - New bullets have structured metadata (ID, helpful/harmful counters)
  - Duplicate lessons are de-duplicated
  - policy.md included in next system prompt
```

### 4.3 Integration Test: Identity Self-Editing

```
Given: Agent receives "your name is Olivia"
When: Agent writes to identity.md
Then:
  - Write succeeds (no veto)
  - Audit event emitted with before/after content
  - JSONL audit log appended at workspace/audit/identity-changes.jsonl
  - Next system prompt reflects updated identity
```

### 4.4 Integration Test: Hybrid Search

```
Given: Agent has 10 days of notes and 5 entities
When: User invokes memory_search with query "project deadline"
Then:
  - Index rebuilt if source files changed
  - BM25 and vector results merged with configured weights
  - Top-K results returned with source, score, and content snippet
```

### 4.5 Unit Test Coverage Targets

| Component | Coverage Target |
|-----------|----------------|
| session_manager.py (core) | >= 90% |
| markdown_memory.py | >= 85% |
| entity_extractor.py | >= 80% |
| hybrid_search.py | >= 80% |
| policy (ACE) logic | >= 85% |

---

## 5. Constraints

- ArcRun is a sibling project — changes to `loop.py` must be minimal (add `messages` parameter only)
- Core LOC must remain < 3,000 total after adding SessionManager
- Embedding model must work offline (air-gapped environments)
- All memory files must be plaintext (markdown, JSONL, JSON) — no opaque binary storage except search.db
- search.db is gitignored and rebuildable from source files
- No PyTorch dependency in production — use ONNX Runtime or sentence-transformers with CPU-only
- ACE policy approach is subject to revision per user instruction ("we can change/update later")
