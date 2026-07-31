# PLAN: Memory Module (Markdown Memory)

**Spec ID**: S002
**Status**: COMPLETE
**Last Updated**: 2026-02-15

---

## Overview

Memory module implemented in 5 phases following dependency order. TDD throughout. Pre-requisite core changes first, then module components, then integration.

## Phases

### Phase 1: Core Pre-requisites (config + errors + session)
### Phase 2: ArcRun + Agent Changes (messages param, chat method)
### Phase 3: Memory Module Foundation (main module + notes + context guard + identity audit)
### Phase 4: Memory Module Intelligence (entity extraction + hybrid search + ACE policy)
### Phase 5: Integration + Quality Gates

---

## Phase 1: Core Pre-requisites

> Config changes, error types, and SessionManager. These unblock everything else.

- [x] **T1.1** Add config sections `[activity: core-development]`
  - [ ] T1.1.1 Write tests for EvalConfig: defaults, provider/model/temperature, fallback_behavior validation
  - [ ] T1.1.2 Write tests for MemoryConfig: defaults, search weights sum validation, budget values
  - [ ] T1.1.3 Write tests for SessionConfig: defaults, retention values
  - [ ] T1.1.4 Write tests for ArcAgentConfig with new sections: load from TOML, env overrides
  - [ ] T1.1.5 Implement EvalConfig, MemoryConfig, SessionConfig Pydantic models
  - [ ] T1.1.6 Add new sections to ArcAgentConfig root model
  - [ ] T1.1.7 Update arcagent.toml.example with new sections
  - [ ] T1.1.8 Verify: `pytest tests/unit/core/test_config.py` passes
  - [ ] T1.1.9 Verify: `mypy arcagent/core/config.py --strict`
  - _Requirements: REQ-CFG-10, REQ-CFG-11, REQ-CFG-12_
  - _Design: SDD Section 2.1_

- [x] **T1.2** Add error types `[activity: core-development]`
  - [ ] T1.2.1 Write tests for new error classes (MemoryError, SessionError, EntityExtractionError, SearchError, PolicyEvalError)
  - [ ] T1.2.2 Implement new error types in errors.py
  - [ ] T1.2.3 Verify: `pytest tests/unit/core/test_errors.py` passes
  - _Requirements: SDD Section 5.1_

- [x] **T1.3** Implement SessionManager `[activity: core-development]`
  - [ ] T1.3.1 Write tests for session creation: UUID4 ID, sessions directory created
  - [ ] T1.3.2 Write tests for message append: thread-safe, JSONL line written
  - [ ] T1.3.3 Write tests for session resume: load from JSONL, skip malformed lines
  - [ ] T1.3.4 Write tests for get_messages: returns snapshot (not reference)
  - [ ] T1.3.5 Write tests for compaction: sliding window (30/70), summary entry in JSONL
  - [ ] T1.3.5a Write tests for pre-compaction flush: key facts extracted from compacted messages, appended to context.md
  - [ ] T1.3.5b Write tests for pre-compaction flush edge cases: empty context.md, context.md at budget limit
  - [ ] T1.3.6 Write tests for session cleanup: retention by count, retention by days
  - [ ] T1.3.7 Write tests for edge cases: empty session, crash-during-append recovery
  - [ ] T1.3.8 Implement SessionManager class
  - [ ] T1.3.9 Implement create_session and resume_session
  - [ ] T1.3.10 Implement append_message with asyncio.Lock + JSONL append
  - [ ] T1.3.11 Implement compact with sliding window + eval model call
  - [ ] T1.3.11a Implement _pre_compact_flush: extract key facts from messages-to-be-compacted, append to context.md (OpenClaw pattern)
  - [ ] T1.3.12 Implement cleanup_old_sessions
  - [ ] T1.3.13 Verify: `pytest tests/unit/core/test_session_manager.py` (target >=90%)
  - [ ] T1.3.14 Verify: `mypy arcagent/core/session_manager.py --strict`
  - _Requirements: REQ-SES-01 through REQ-SES-08, REQ-SES-05-A_
  - _Design: SDD Section 2.2_

**Phase 1 gate:** Config loads new sections. SessionManager creates, appends, resumes, and compacts. `mypy` and `ruff` clean. Core LOC still < 3,000.

**Completion: 3/3 tasks | Remaining: 0**

---

## Phase 2: ArcRun + Agent Changes `[blocked-by: T1.1, T1.2, T1.3]`

> Add messages parameter to ArcRun. Add chat() method and SessionManager integration to ArcAgent.

- [x] **T2.1** ArcRun messages parameter `[activity: core-development]`
  - [ ] T2.1.1 Write tests for run() with messages=None: existing behavior unchanged
  - [ ] T2.1.2 Write tests for run() with messages provided: system prompt prepended fresh
  - [ ] T2.1.3 Write tests for messages without system message: system prompt still added
  - [ ] T2.1.4 Modify arcrun loop.py _build_state to accept messages parameter
  - [ ] T2.1.5 Modify arcrun run() signature to accept messages parameter
  - [ ] T2.1.6 Verify: `pytest` in arcrun project passes
  - _Requirements: REQ-RUN-01, REQ-RUN-02_
  - _Design: SDD Section 4.1_

- [x] **T2.2** ContextManager assemble_prompt event `[activity: core-development]`
  - [ ] T2.2.1 Write tests for assemble_system_prompt: now async, emits agent:assemble_prompt
  - [ ] T2.2.2 Write tests for section injection: handler adds "notes" section
  - [ ] T2.2.3 Write tests for handler failure: prompt still assembled (best-effort injection)
  - [ ] T2.2.4 Write tests for ordering: identity, notes, policy, context
  - [ ] T2.2.5 Modify assemble_system_prompt to async + emit event
  - [ ] T2.2.6 Add bus parameter to ContextManager constructor
  - [ ] T2.2.7 Update callers of assemble_system_prompt (agent.py) to await
  - [ ] T2.2.8 Verify: `pytest tests/unit/core/test_context_manager.py` passes
  - [ ] T2.2.9 Verify: existing integration tests still pass
  - _Requirements: REQ-CTX-10, REQ-CTX-11, REQ-CTX-12_
  - _Design: SDD Section 2.3_

- [x] **T2.3** ArcAgent chat() method `[activity: core-development]` `[blocked-by: T2.1, T2.2]`
  - [ ] T2.3.1 Write tests for chat(): creates session, appends messages, calls arcrun.run with messages
  - [ ] T2.3.2 Write tests for chat() with session_id: resumes existing session
  - [ ] T2.3.3 Write tests for chat() session persistence: messages in JSONL after chat
  - [ ] T2.3.4 Write tests for startup: SessionManager initialized in correct order
  - [ ] T2.3.5 Write tests for shutdown: SessionManager cleaned up
  - [ ] T2.3.6 Implement chat() method on ArcAgent
  - [ ] T2.3.7 Integrate SessionManager into ArcAgent startup/shutdown
  - [ ] T2.3.8 Pass session messages to arcrun.run() via messages parameter
  - [ ] T2.3.9 Verify: `pytest tests/unit/core/test_agent.py` passes
  - [ ] T2.3.10 Verify: `mypy arcagent/core/agent.py --strict`
  - _Requirements: REQ-AGT-10, REQ-AGT-11, REQ-AGT-12_
  - _Design: SDD Section 2.4_

**Phase 2 gate:** `chat()` works end-to-end with message persistence. ArcRun accepts messages. ContextManager emits assemble_prompt event. All existing tests still pass.

**Completion: 3/3 tasks | Remaining: 0**

---

## Phase 3: Memory Module Foundation `[blocked-by: T2.2, T2.3]`

> Main module, notes management, context guard, identity audit. These are the hook-based components.

- [x] **T3.1** Main module scaffolding `[activity: module-development]`
  - [x] T3.1.1 Create `arcagent/modules/memory/` package structure
  - [x] T3.1.2 Create MODULE.yaml manifest
  - [x] T3.1.3 Write tests for Module protocol implementation: name, startup, shutdown
  - [x] T3.1.4 Write tests for event subscription: pre_tool, post_tool, assemble_prompt, post_respond
  - [x] T3.1.5 Write tests for path resolution: read/write/edit tools, canonicalization
  - [x] T3.1.6 Write tests for bash command parsing: echo >, rm, mv targeting memory paths
  - [x] T3.1.7 Write tests for re-entrancy guard: nested hooks don't fire
  - [x] T3.1.8 Write tests for background task tracking: task set, done callback, error logging
  - [x] T3.1.9 Write tests for shutdown: cancels background tasks, closes resources
  - [x] T3.1.10 Implement MarkdownMemoryModule class
  - [x] T3.1.11 Implement path resolution and routing logic
  - [x] T3.1.12 Implement bash command parser for memory path detection
  - [x] T3.1.13 Implement background task management (_spawn_background)
  - [x] T3.1.14 Verify: `pytest tests/unit/modules/memory/test_markdown_memory.py` (target >=85%)
  - _Requirements: REQ-MEM-01 through REQ-MEM-11_
  - _Design: SDD Section 3.1_

- [x] **T3.2** Notes management `[activity: module-development]` `[parallel: true]`
  - [x] T3.2.1 Write tests for append-only enforcement: write tool vetoed, edit tool allowed, read allowed
  - [x] T3.2.2 Write tests for bash bypass: echo > notes/file.md vetoed
  - [x] T3.2.3 Write tests for veto message: clear explanation returned
  - [x] T3.2.4 Write tests for get_recent_notes: today + yesterday content, missing files handled
  - [x] T3.2.5 Write tests for token budget on injected notes
  - [x] T3.2.6 Implement NoteManager class
  - [x] T3.2.7 Implement enforce_append_only with veto
  - [x] T3.2.8 Implement get_recent_notes with token budgets
  - [x] T3.2.9 Verify: tests pass
  - _Requirements: REQ-NOT-01 through REQ-NOT-06_
  - _Design: SDD Section 3.1 (NoteManager)_

- [x] **T3.3** Context.md guard `[activity: module-development]` `[parallel: true]`
  - [x] T3.3.1 Write tests for budget enforcement: content under budget passes
  - [x] T3.3.2 Write tests for over-budget: auto-truncation from top (oldest entries)
  - [x] T3.3.3 Write tests for edge cases: empty content, single line over budget
  - [x] T3.3.4 Implement ContextGuard class
  - [x] T3.3.5 Implement enforce_budget with auto-truncation
  - [x] T3.3.6 Verify: tests pass
  - _Requirements: REQ-WM-01 through REQ-WM-03_
  - _Design: SDD Section 3.1 (ContextGuard)_

- [x] **T3.4** Identity audit `[activity: module-development]` `[parallel: true]`
  - [x] T3.4.1 Write tests for capture_before: snapshots current content
  - [x] T3.4.2 Write tests for capture_after: emits audit event with before/after
  - [x] T3.4.3 Write tests for JSONL audit file: appends entry with all required fields
  - [x] T3.4.4 Write tests for no-change detection: same content → no audit event
  - [x] T3.4.5 Write tests for first write: no existing file → empty "before"
  - [x] T3.4.6 Implement IdentityAuditor class
  - [x] T3.4.7 Implement capture_before and capture_after
  - [x] T3.4.8 Implement JSONL audit file writing
  - [x] T3.4.9 Verify: tests pass
  - _Requirements: REQ-IDT-01 through REQ-IDT-03_
  - _Design: SDD Section 3.1 (IdentityAuditor)_

**Phase 3 gate:** Memory module loads via Module Bus. Notes append-only enforced. Context.md budget enforced. Identity changes audited. Hook routing works for all tool types including bash.

**Completion: 4/4 tasks | Remaining: 0**

---

## Phase 4: Memory Module Intelligence `[blocked-by: T3.1]`

> Entity extraction, hybrid search, and ACE policy engine. These are the LLM-dependent and search components.

- [x] **T4.1** Entity extraction `[activity: module-development]`
  - [x] T4.1.1 Write tests for extraction prompt: structured output schema
  - [x] T4.1.2 Write tests for trivial exchange skip: short messages produce no entities
  - [x] T4.1.3 Write tests for new entity creation: directory, facts.jsonl, summary.md, index update
  - [x] T4.1.4 Write tests for existing entity update: append fact, index update
  - [x] T4.1.5 Write tests for contradiction detection: supersedes chain, old fact marked
  - [x] T4.1.6 Write tests for case-insensitive matching: "josh" matches "Josh"
  - [x] T4.1.7 Write tests for alias matching: "Mr. Schultz" matches entity with alias
  - [x] T4.1.8 Write tests for atomic index write: write-to-temp + rename
  - [ ] T4.1.9 Write tests for concurrent extraction: semaphore limits to max 2 _(deferred: semaphore in module, not extractor)_
  - [x] T4.1.10 Write tests for eval model failure: graceful skip (fallback_behavior=skip)
  - [x] T4.1.11 Implement EntityExtractor class
  - [x] T4.1.12 Implement extract method with eval model call
  - [x] T4.1.13 Implement index management with atomic writes and asyncio.Lock
  - [x] T4.1.14 Implement facts append with contradiction detection
  - [x] T4.1.15 Implement entity matching (case-insensitive + alias)
  - [ ] T4.1.16 Set up VCR.py cassettes for LLM response recording _(deferred: using AsyncMock)_
  - [x] T4.1.17 Verify: `pytest tests/unit/modules/memory/test_entity_extractor.py` (target >=80%)
  - _Requirements: REQ-ENT-01 through REQ-ENT-09_
  - _Design: SDD Section 3.2_

- [x] **T4.2** Hybrid search `[activity: module-development]` `[parallel: true]`
  - [x] T4.2.1 Write tests for SQLite schema creation: fts5, vec0 (if available), metadata tables
  - [x] T4.2.2 Write tests for BM25 search: keyword matching, ranking
  - [ ] T4.2.3 Write tests for vector search: embedding generation, cosine similarity _(deferred: sqlite-vec optional)_
  - [ ] T4.2.4 Write tests for hybrid merge: weighted combination, deduplication _(deferred: BM25-only MVP)_
  - [x] T4.2.4a Write tests for scope parameter: search limited to notes/entities/context/sessions when specified
  - [ ] T4.2.4b Write tests for date filtering: date_from and date_to constrain results _(deferred: Phase 5)_
  - [x] T4.2.5 Write tests for lazy reindex: only rebuild when files changed (mtime check)
  - [x] T4.2.6 Write tests for document chunking: ~400 tokens, heading boundaries
  - [ ] T4.2.7 Write tests for lazy model loading: model loaded on first vector search _(deferred: sqlite-vec optional)_
  - [x] T4.2.8 Write tests for sqlite-vec fallback: BM25-only when extension unavailable
  - [x] T4.2.9 Write tests for rebuild: full reindex from scratch
  - [x] T4.2.10 Write tests for WAL mode: concurrent reads during writes
  - [x] T4.2.11 Implement HybridSearch class with SQLite setup
  - [x] T4.2.12 Implement BM25 search via FTS5
  - [ ] T4.2.13 Implement vector search via sqlite-vec _(deferred: BM25-only MVP)_
  - [x] T4.2.14 Implement lazy reindexing with file mtime tracking
  - [x] T4.2.15 Implement document chunking
  - [ ] T4.2.16 Implement lazy embedding model loading _(deferred: sqlite-vec optional)_
  - [ ] T4.2.17 Implement result merging with configurable weights _(deferred: BM25-only MVP)_
  - [x] T4.2.17a Implement scope filtering (restrict search to specified memory tier)
  - [ ] T4.2.17b Implement date range filtering (date_from, date_to) _(deferred: Phase 5)_
  - [ ] T4.2.18 Register memory_search as native tool _(Phase 5: integration)_
  - [x] T4.2.19 Verify: `pytest tests/unit/modules/memory/test_hybrid_search.py` (target >=80%)
  - _Requirements: REQ-SRC-01 through REQ-SRC-12, REQ-SRC-03-A, REQ-SRC-03-B_
  - _Design: SDD Section 3.3_

- [x] **T4.3** ACE Policy Engine `[activity: module-development]` `[parallel: true]`
  - [x] T4.3.1 Write tests for policy bullet parsing: extract ID, text, score (1-10), uses, reviewed, created, source from markdown
  - [x] T4.3.2 Write tests for policy serialization: render bullets back to structured markdown format
  - [x] T4.3.3 Write tests for ACE Reflector: eval model call produces PolicyDelta with score_delta (+1/-2)
  - [x] T4.3.4 Write tests for Curator ADD: new bullet starts at score 5, source = session_id
  - [x] T4.3.5 Write tests for Curator UPDATE: apply score_delta, increment uses, update source
  - [x] T4.3.6 Write tests for Curator REWRITE: update text, preserve ID, score, and source
  - [x] T4.3.7 Write tests for score thresholds: <=2 remove, 3-4 at-risk, 5-7 improve, 8-10 promote
  - [x] T4.3.7a Write tests for asymmetric scoring: positive feedback +1, negative feedback -2
  - [ ] T4.3.8 Write tests for de-duplication: cosine > 0.85 → merge _(deferred: requires embeddings)_
  - [ ] T4.3.9 Write tests for decay: score -1 every 30 days of non-use _(deferred: Phase 5)_
  - [x] T4.3.10 Write tests for eval model failure: skip with fallback_behavior=skip
  - [x] T4.3.11 Write tests for empty policy: first evaluation creates initial bullets at score 5
  - [x] T4.3.12 Write tests for atomic policy write: write-to-temp + rename
  - [x] T4.3.12a Write tests for source tracking: session_id recorded on create and update
  - [x] T4.3.13 Implement PolicyEngine class
  - [x] T4.3.14 Implement ACE Reflector (_reflect method) with score guidance in prompt
  - [x] T4.3.15 Implement ACE Curator (_curate method) with score-based operations
  - [x] T4.3.16 Implement policy parsing and serialization (structured bullet format with metadata)
  - [ ] T4.3.17 Implement de-duplication via semantic similarity _(deferred: requires embeddings)_
  - [ ] T4.3.18 Implement decay logic (score -1 per 30 days non-use) _(deferred: Phase 5)_
  - [ ] T4.3.19 Set up VCR.py cassettes for policy evaluation LLM responses _(deferred: using AsyncMock)_
  - [x] T4.3.20 Verify: `pytest tests/unit/modules/memory/test_policy_engine.py` (target >=85%)
  - _Requirements: REQ-POL-01 through REQ-POL-11_
  - _Design: SDD Section 3.4_

**Phase 4 gate:** Entity extraction works async. Hybrid search returns BM25-ranked results. ACE policy engine evaluates and updates policy.md. Deferred items: vector search (sqlite-vec optional), dedup/decay (require embeddings), VCR cassettes (using AsyncMock).

**Completion: 3/3 tasks | Remaining: 0**

---

## Phase 5: Integration + Quality Gates `[blocked-by: Phase 3, Phase 4]`

> Wire everything together. Integration tests. Quality verification.

- [x] **T5.1** Integration tests `[activity: integration-testing]`
  - [x] T5.1.1 Write integration test: module startup and handler registration (2 tests)
  - [x] T5.1.2 Write integration test: identity self-editing with audit trail
  - [x] T5.1.3 Write integration test: notes append-only enforcement end-to-end (3 tests: write vetoed, edit allowed, bash vetoed)
  - [x] T5.1.4 Write integration test: entity extraction after post_respond
  - [x] T5.1.5 Write integration test: hybrid search across notes + entities
  - [x] T5.1.6 Write integration test: ACE policy evaluation cycle
  - [ ] T5.1.7 Write integration test: session compaction (sliding window) _(deferred: requires multi-turn chat test harness)_
  - [x] T5.1.8 Write integration test: context.md budget enforcement
  - [x] T5.1.9 Write integration test: bash bypass blocked for notes
  - [x] T5.1.10 Write integration test: non-memory paths not intercepted
  - [x] T5.1.11 Verify: `pytest tests/integration/test_memory_integration.py` passes (15 tests)
  - _Requirements: PRD Section 4 (all acceptance criteria)_

- [x] **T5.2** Module registration `[activity: module-development]` `[blocked-by: T5.1]`
  - [x] T5.2.1 Register MarkdownMemoryModule in agent.py startup (when config.modules.memory enabled)
  - [x] T5.2.2 Ensure module loads and subscribes during agent startup
  - [x] T5.2.3 Write test: agent startup with memory module enabled
  - [x] T5.2.4 Write test: agent startup with memory module disabled (no hooks registered)
  - [x] T5.2.5 Write test: agent startup without memory config (no module loaded)

- [x] **T5.3** Quality gates `[activity: core-development]` `[blocked-by: T5.2]`
  - [x] T5.3.1 Verify: core LOC = 2,266 (under 3,000 budget)
  - [x] T5.3.2 Verify: memory module LOC = 1,219 (under 1,500 budget)
  - [x] T5.3.3 Verify: `pytest --cov=arcagent` = 93.67% (above 80% threshold)
  - [x] T5.3.4 Verify: Per-component coverage — core >=90%, memory >=91%
  - [x] T5.3.5 Verify: `mypy arcagent/ --strict` passes with 0 errors (24 files)
  - [x] T5.3.6 Verify: `ruff check` passes on all modified files (0 errors)
  - [x] T5.3.7 Verify: All 423 tests pass (no regressions from S001)
  - _Requirements: NFR-10 through NFR-18, Quality Gates_

**Phase 5 gate:** All integration tests pass (15). Module loads cleanly. All quality gates met. Core 2,266 LOC. Coverage 93.67%. No regressions.

**Completion: 3/3 tasks | Remaining: 0**

---

## Summary

| Phase | Tasks | Parallel | Dependencies |
|-------|-------|----------|--------------|
| 1: Core Pre-requisites | 3 | No | None |
| 2: ArcRun + Agent | 3 | Partial (T2.3 after T2.1+T2.2) | Phase 1 |
| 3: Module Foundation | 4 | Yes (T3.2, T3.3, T3.4 after T3.1) | Phase 2 |
| 4: Module Intelligence | 3 | Yes (T4.1, T4.2, T4.3 can parallel) | T3.1 |
| 5: Integration + QA | 3 | Sequential | Phase 3 + Phase 4 |

**Total tasks: 16 | Total subtasks: ~155**
**Completion: 16/16 tasks | Remaining: 0**
