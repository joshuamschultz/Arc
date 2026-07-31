# PLAN: Scheduling / Heartbeat / Cron

**Spec**: SPEC-002 | **Status**: COMPLETE | **Date**: 2026-02-16

## Summary

6 phases, TDD throughout. Each phase produces working, tested code before the next begins.

**Estimated files**: 10 new, 2 modified
**Test strategy**: Unit tests with frozen time (70%), integration test with mock LLM (30%)

---

## Phase 1: Models + Config (Foundation)

**Goal**: Pydantic models and config wired into ArcAgentConfig.

### Tasks

- [x] **1.1** Create `arcagent/modules/scheduler/models.py`
  - `ActiveHours` model with start/end/timezone validation
  - `ScheduleMetadata` model with all audit fields
  - `ScheduleEntry` model with type-discriminated validation
  - Validators: cron expression (croniter), ISO datetime, minimum interval, prompt length
  - Prompt injection pattern detection validator

- [x] **1.2** Add `SchedulerConfig` to `arcagent/core/config.py`
  - Add `SchedulerConfig` class after `MemoryConfig`
  - Add `scheduler: SchedulerConfig = SchedulerConfig()` to `ArcAgentConfig`

- [x] **1.3** Create `arcagent/modules/scheduler/__init__.py`
  - Empty module init (exports added in Phase 4)

- [x] **1.4** Write unit tests for models
  - `tests/unit/modules/scheduler/test_models.py`
  - Valid/invalid cron expressions
  - Valid/invalid interval (below minimum)
  - Valid/invalid once datetime (naive vs aware)
  - Prompt validation (length, injection patterns)
  - ActiveHours validation
  - ScheduleEntry serialization round-trip

**Completion**: Models importable, config wired, all model tests pass.

---

## Phase 2: Store (Persistence)

**Goal**: Atomic JSON file read/write with CRUD operations.

### Tasks

- [x] **2.1** Create `arcagent/modules/scheduler/store.py`
  - `ScheduleStore` class with `path: Path` constructor
  - `load() -> list[ScheduleEntry]` (returns empty list if file missing)
  - `save(entries: list[ScheduleEntry])` with atomic write (mkstemp + fsync + replace)
  - `add(entry: ScheduleEntry)` — load, append, save
  - `update(id: str, updates: dict) -> ScheduleEntry` — load, find, merge, save
  - `remove(id: str)` — load, filter, save
  - `get(id: str) -> ScheduleEntry | None` — load, find

- [x] **2.2** Write unit tests for store
  - `tests/unit/modules/scheduler/test_store.py`
  - Load from empty/missing file
  - Save and load round-trip
  - Add entry, verify persisted
  - Update entry fields
  - Remove entry
  - Get by ID (found/not found)
  - Atomic write: verify no partial writes (simulate crash via mock)
  - File permissions on created file

**Completion**: Store CRUD works, atomic writes verified, all store tests pass.

---

## Phase 3: Scheduler Engine (Timer + Queue)

**Goal**: Core scheduling logic — evaluation, timing, sequential execution.

### Tasks

- [x] **3.1** Create `arcagent/modules/scheduler/scheduler.py`
  - `SchedulerEngine` class
  - Constructor: `store`, `config`, `telemetry`, `agent_run_fn` (callback)
  - `start()` — create asyncio timer task + worker task
  - `stop()` — cancel timer, drain queue via `queue.join()` with timeout
  - `_timer_loop()` — sleep, evaluate schedules, enqueue fires
  - `_worker()` — dequeue, execute via `agent_run_fn(prompt)` with timeout
  - `_should_fire(entry)` — type-specific evaluation (cron/interval/once)
  - `_is_within_active_hours(entry)` — timezone-aware active hours check
  - `_on_execution_complete(entry, result)` — update metadata, persist
  - `_on_execution_failed(entry, error)` — update metadata, check circuit breaker
  - Circuit breaker logic: consecutive failures → disable schedule

- [x] **3.2** Write unit tests for engine
  - `tests/unit/modules/scheduler/test_scheduler.py`
  - Uses `freezegun` or manual time mocking
  - Cron evaluation: next fire time calculation
  - Interval evaluation: fires after elapsed time
  - Once evaluation: fires at specified time, skips if already run
  - Active hours: within range, outside range, timezone conversion
  - DST handling: spring forward, fall back (croniter + pytz)
  - Queue: sequential execution (mock agent_run_fn)
  - Circuit breaker: 3 failures → disable
  - Graceful shutdown: queue drains before stop

**Completion**: Engine starts, evaluates schedules, fires correctly, handles failures. All engine tests pass.

---

## Phase 4: Tools (Agent-Facing CRUD)

**Goal**: 4 RegisteredTool instances the LLM can call.

### Tasks

- [x] **4.1** Create `arcagent/modules/scheduler/tools.py`
  - `create_scheduler_tools(module: SchedulerModule) -> list[RegisteredTool]`
  - `schedule_create` tool with JSON Schema input
  - `schedule_list` tool with optional `enabled_only` filter
  - `schedule_update` tool with partial update support
  - `schedule_cancel` tool with optional `delete` flag
  - All tools validate inputs, emit events, return JSON strings

- [x] **4.2** Write unit tests for tools
  - `tests/unit/modules/scheduler/test_tools.py`
  - Create each schedule type (cron, interval, once)
  - Create with active_hours
  - Create exceeds quota → error
  - Create with invalid cron → error
  - Create with injection prompt → error
  - List all vs enabled_only
  - Update timing, prompt, enable/disable
  - Update nonexistent → error
  - Cancel (disable) vs cancel (delete)

**Completion**: All 4 tools work, validated, emit events. All tool tests pass.

---

## Phase 5: Module + CLI (Integration)

**Goal**: Wire everything together as a Module Bus participant. Add `arc agent serve`.

### Tasks

- [x] **5.1** Complete `arcagent/modules/scheduler/__init__.py`
  - `SchedulerModule` class satisfying `Module` Protocol
  - Constructor with DI params: `config`, `telemetry`, `workspace`
  - `startup()`: register tools, subscribe to `agent:shutdown`, create engine, start engine
  - `shutdown()`: stop engine, persist state
  - Engine needs reference to agent for `agent.run()` — receive via startup context or deferred binding

- [x] **5.2** Create `arcagent/modules/scheduler/MODULE.yaml`
  - Manifest following memory module pattern
  - Events: subscribes to `agent:shutdown`, emits schedule:* events

- [x] **5.3** Add `arc agent serve` to `arccli/src/arccli/agent.py`
  - `@agent.command("serve")` with path argument
  - Manual event loop (not asyncio.run) for graceful shutdown
  - Signal handlers: SIGTERM, SIGINT → set shutdown event
  - Async serve function: startup agent, wait for shutdown, cleanup
  - Verbose mode: log schedule fires and results

- [x] **5.4** Write unit tests for module lifecycle
  - `tests/unit/modules/scheduler/test_module.py`
  - Module satisfies Protocol (name, startup, shutdown)
  - Startup registers 4 tools
  - Startup subscribes to agent:shutdown
  - Shutdown stops engine cleanly

**Completion**: Module loads via convention, tools appear in registry, `arc agent serve` runs. All module tests pass.

---

## Phase 6: Integration Tests

**Goal**: End-to-end verification with mock LLM provider.

### Tasks

- [x] **6.1** Write integration test
  - `tests/integration/test_scheduler_integration.py`
  - Create agent with scheduler module enabled
  - Create interval schedule via tool
  - Advance time, verify schedule fires
  - Verify `agent.run()` called with correct prompt
  - Verify metadata updated (last_run, run_count)
  - Verify events emitted
  - Verify graceful shutdown drains queue

- [x] **6.2** Verify quality gates
  - `ruff check` passes on all new files — 0 errors
  - Coverage >= 80% line on scheduler module — 89%
  - No hardcoded secrets or credentials

**Completion**: Full end-to-end works. Quality gates pass. SPEC-002 status → COMPLETE.

---

## Success Criteria

| Criteria | Measurement |
|----------|-------------|
| All 3 schedule types work | Unit tests for cron, interval, once evaluation |
| 4 CRUD tools operational | Tool tests cover create, list, update, cancel |
| Sequential execution | Queue test with multiple concurrent fires |
| Atomic persistence | Store test verifies no partial writes |
| DST handling | Cron test with spring-forward/fall-back |
| Circuit breaker | 3 failure test → schedule disabled |
| Graceful shutdown | Integration test drains queue on SIGTERM |
| Module loads by convention | Integration test: agent startup includes scheduler |
| `arc agent serve` runs | CLI test: command exists and starts agent |
| Quality gates | ruff: 0 errors, mypy: 0 errors, coverage >= 80% |

## Dependencies Between Phases

```
Phase 1 (Models + Config) ─┐
                            ├─ Phase 2 (Store) ─┐
                            │                    ├─ Phase 3 (Engine) ─┐
                            │                    │                    ├─ Phase 4 (Tools)
                            │                    │                    │       │
                            │                    │                    │       v
                            │                    │                    └─ Phase 5 (Module + CLI)
                            │                    │                              │
                            │                    │                              v
                            └────────────────────┴─────────────────── Phase 6 (Integration)
```

Phases 1→2→3 are strictly sequential. Phase 4 depends on 3. Phase 5 depends on 3+4. Phase 6 depends on all.
