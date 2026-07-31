# PRD: Memory Wiring

**Spec ID**: S004
**Status**: PENDING

---

## Problem Statement

ArcAgent's memory system is fully built (423 tests, 93.67% coverage) but completely non-functional. Five disconnected gaps prevent any memory operation from executing:

1. The eval model is never instantiated — entity extraction, policy evaluation, and compaction summarization all silently skip
2. The `memory_search` hybrid search engine exists but is never registered as a tool the agent can call
3. The agent's identity.md doesn't mention memory capabilities — the agent doesn't know it can remember things
4. Compaction is never triggered — sessions grow unbounded, eventually hitting emergency truncation
5. The Basic Agent config doesn't include `[modules.memory]` — the module never loads

Additionally, the module loading system is hardcoded in `agent.py:_register_modules()`, which prevents future modules from being added without modifying core code.

## User Stories

### US-1: Agent Remembers Across Sessions
**As** a developer deploying an ArcAgent,
**I want** the agent to automatically remember facts, entities, and context across sessions,
**So that** it provides continuity without manual memory management.

**Acceptance Criteria:**
- Agent starts with memory module loaded automatically
- Entity extraction fires after each response (when configured)
- Policy evaluation fires at configured intervals
- Memory search is available as a callable tool
- Agent identity.md describes memory capabilities

### US-2: Convention-Based Module Loading
**As** a module developer,
**I want** to drop a module folder with MODULE.yaml into `arcagent/modules/` and enable it in config,
**So that** I don't need to modify agent.py to register new modules.

**Acceptance Criteria:**
- MODULE.yaml `entry_point` field used for import
- Config `[modules.{name}] enabled = true` controls loading
- Malformed MODULE.yaml with missing `entry_point` fails startup with clear error
- Optional fields missing → warning, continue loading

### US-3: Automatic Compaction
**As** an agent running long conversations,
**I want** my session to automatically compact when context grows large,
**So that** I don't hit emergency truncation and lose information.

**Acceptance Criteria:**
- Session checks context ratio after each turn
- Compaction triggers at compact_threshold (0.85)
- Pre-flush extracts key facts to context.md before compaction (MAGMA pattern)
- asyncio.Lock prevents race conditions during compaction
- Emergency truncation (0.95) remains as safety net

### US-4: Proper Module Dependency Injection
**As** a module author,
**I want** to receive all my dependencies via a typed context object,
**So that** I can access tool_registry, bus, config, and telemetry without service locator patterns.

**Acceptance Criteria:**
- ModuleContext dataclass provides bus, tool_registry, config, telemetry, workspace, llm_config
- ModuleContext is frozen (immutable references)
- Module.startup() receives ModuleContext instead of bare ModuleBus
- Memory module updated to use ModuleContext

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Decision |
|----|-------------|----------|----------|
| REQ-W-01 | Memory module creates eval model lazily from EvalConfig on first use | Must | MW-001 |
| REQ-W-02 | Eval model falls back to agent's LLM config when EvalConfig.provider is empty | Must | MW-012 |
| REQ-W-03 | Convention loader discovers modules from `arcagent/modules/*/MODULE.yaml` | Must | MW-002, MW-007 |
| REQ-W-04 | Convention loader checks `[modules.{name}] enabled` in config before loading | Must | MW-002 |
| REQ-W-05 | Convention loader fails startup on missing critical fields (entry_point, name) | Must | MW-007 |
| REQ-W-06 | Convention loader warns on missing optional fields | Should | MW-007 |
| REQ-W-07 | ModuleContext dataclass with bus, tool_registry, config, telemetry, workspace, llm_config | Must | MW-004 |
| REQ-W-08 | Module.startup(ctx: ModuleContext) replaces Module.startup(bus: ModuleBus) | Must | MW-004 |
| REQ-W-09 | Memory module registers memory_search tool during startup via ModuleContext | Must | MW-003 |
| REQ-W-10 | memory_search accepts query, scope, date_from, date_to parameters | Must | MW-011 |
| REQ-W-11 | Date filtering uses FTS5 UNINDEXED columns (SQL-level, not post-filter) | Should | MW-011 |
| REQ-W-12 | Memory module injects default behavioral guidance via assemble_prompt hook | Must | MW-009 |
| REQ-W-13 | identity.md `## Memory` section overrides module-injected guidance | Must | MW-009 |
| REQ-W-14 | Memory enabled by default in new configs | Must | MW-010 |
| REQ-W-15 | Basic_Agent/arcagent.toml includes `[modules.memory] enabled = true` | Must | MW-010 |
| REQ-W-16 | Session checks context ratio after each turn via chat() | Must | MW-006 |
| REQ-W-17 | Session triggers compaction at compact_threshold (0.85) | Must | MW-006 |
| REQ-W-18 | Pre-compaction flush uses hybrid fast/slow path (MAGMA pattern) | Should | MW-006 |
| REQ-W-19 | asyncio.Lock protects context during compaction | Must | MW-006 |
| REQ-W-20 | Background tasks limited by asyncio.Semaphore(max_concurrent=2) | Must | MW-015 |
| REQ-W-21 | Session owns ContextManager instance | Must | MW-008 |
| REQ-W-22 | agent.py _register_modules() replaced by convention loader | Must | MW-007 |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-W-01 | Eval model lazy init adds 0 startup latency | < 1ms overhead at startup |
| NFR-W-02 | Convention loader scans in < 100ms | < 100ms for 10 modules |
| NFR-W-03 | memory_search with date filter | < 30ms for BM25 path |
| NFR-W-04 | ModuleContext creation | < 1ms |
| NFR-W-05 | All new code passes mypy --strict | 0 errors |
| NFR-W-06 | All new code passes ruff check | 0 errors |
| NFR-W-07 | New code coverage >= 90% | Per core component |

## Out of Scope

- Modifying existing memory module internals (entity extraction logic, policy engine, note manager)
- Building new search backends
- Multi-agent memory sharing
- Module signing
- Changes to ArcRun or ArcLLM

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Memory module loads on startup | 100% of configured agents | Startup log: "Registered memory module" |
| Entity extraction fires | After each response when configured | Telemetry: memory.entity_extraction events |
| memory_search callable | Agent can invoke tool | Tool registry contains memory_search |
| Compaction triggers | At 0.85 threshold | Telemetry: session.compaction events |
| Zero regression | All 423 existing tests pass | pytest exit 0 |
