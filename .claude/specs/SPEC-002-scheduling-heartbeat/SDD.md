# SDD: Scheduling / Heartbeat / Cron

**Spec**: SPEC-002 | **Date**: 2026-02-16

## Architecture Overview

The scheduler is a Module Bus participant living in `arcagent/modules/scheduler/`. It is opt-in via config, loaded by the existing `ModuleLoader` convention, and exposes 4 tools to the LLM. A new `arc agent serve` CLI command runs the agent as a long-lived daemon with the scheduler active.

```
arc agent serve
    |
    v
ArcAgent (warm, long-running)
    |
    v
SchedulerModule (Module Bus)
    |
    +-- ScheduleStore: loads/saves workspace/schedules.json (atomic)
    +-- SchedulerEngine: asyncio timer loop + croniter evaluation
    +-- ExecutionQueue: asyncio.Queue, FIFO, one-at-a-time
    |
    v (when schedule fires)
    |
    +-- emit schedule:fired event
    +-- queue execution
    +-- agent.run(prompt) with fresh session
    +-- emit schedule:completed or schedule:failed
    +-- update metadata (last_run, last_result, run_count)
    +-- persist to schedules.json
```

## Components

### 1. Data Models (`arcagent/modules/scheduler/models.py`)

Pydantic v2 models matching the design doc dataclass with build decision refinements.

```python
class ActiveHours(BaseModel):
    start: str  # "HH:MM" format
    end: str    # "HH:MM" format
    timezone: str = "UTC"

class ScheduleMetadata(BaseModel):
    created_by: Literal["agent", "admin", "user", "system"] = "agent"
    created_at: str = ""
    reason: str = ""
    source_session: str = ""
    last_run: str | None = None
    last_result: Literal["ok", "action_taken", "error", None] = None
    run_count: int = 0
    last_duration_seconds: float | None = None
    last_tokens_used: int | None = None
    last_cost_usd: float | None = None

class ScheduleEntry(BaseModel):
    id: str  # Auto-generated UUID prefix
    type: Literal["cron", "interval", "once"]
    prompt: str  # What the agent does when triggered
    enabled: bool = True

    # Type-specific
    expression: str | None = None   # Cron (type="cron")
    at: str | None = None           # ISO datetime (type="once")
    every_seconds: int | None = None  # Seconds (type="interval")

    # Constraints
    active_hours: ActiveHours | None = None
    timeout_seconds: int = 300

    # Audit
    metadata: ScheduleMetadata = ScheduleMetadata()
```

**Validation rules** (Pydantic validators):
- `expression` required when `type == "cron"`, validated via `croniter.is_valid()`
- `at` required when `type == "once"`, must be ISO 8601 with timezone
- `every_seconds` required when `type == "interval"`, minimum 60
- `prompt` max 500 chars, validated against injection patterns (SEC-01)
- `id` auto-generated as `sched_{uuid4_prefix}` if not provided

### 2. Config (`arcagent/core/config.py` addition)

```python
class SchedulerConfig(BaseModel):
    """Configuration for the scheduler module."""
    check_interval_seconds: int = 30      # How often the timer loop checks
    max_schedules: int = 50               # Per-agent limit
    min_interval_seconds: int = 60        # Minimum allowed interval
    max_prompt_length: int = 500          # Prompt injection surface limit
    execution_timeout_seconds: int = 300  # Default per-execution timeout
    max_consecutive_failures: int = 3     # Circuit breaker threshold
    circuit_open_seconds: int = 300       # Circuit breaker cooldown
```

Added to `ArcAgentConfig` as: `scheduler: SchedulerConfig = SchedulerConfig()`

TOML enablement: `[modules.scheduler]` with `enabled = true`.

### 3. Store (`arcagent/modules/scheduler/store.py`)

JSON file persistence with atomic writes.

**Key operations**:
- `load() -> list[ScheduleEntry]`: Read `workspace/schedules.json`, return empty list if missing
- `save(entries: list[ScheduleEntry])`: Atomic write via temp file + `os.fsync()` + `os.replace()`
- `add(entry: ScheduleEntry)`: Load, append, save
- `update(id: str, updates: dict)`: Load, find by id, merge updates, save
- `remove(id: str)`: Load, filter out, save

**Atomic write pattern** (from research):
```python
fd, tmp_path = tempfile.mkstemp(
    dir=self._path.parent,
    prefix=f".{self._path.name}.",
    suffix=".tmp"
)
os.write(fd, json_bytes)
os.fsync(fd)
os.close(fd)
os.replace(tmp_path, self._path)
```

**TOCTOU safety**: Single-process sequential design means no concurrent reads/writes to the JSON file. The store is only accessed from the scheduler module's event loop (never from parallel tasks). Safe for MVP without file locking.

### 4. Scheduler Engine (`arcagent/modules/scheduler/scheduler.py`)

Core timer loop using `asyncio.sleep()` pattern.

**Timer loop**:
```
while running:
    sleep(min(check_interval, time_to_next_fire))
    for each enabled schedule:
        if should_fire(schedule):
            if within_active_hours(schedule):
                queue.put(schedule)
            else:
                emit schedule:skipped (outside active hours)
```

**Schedule evaluation**:
- **cron**: `croniter(expression, last_run or now).get_next()` with DST-aware timezone handling via `pytz.localize()` + `tz.normalize()`
- **interval**: `last_run + timedelta(seconds=every_seconds) <= now`
- **once**: `at <= now` and `run_count == 0`

**Execution queue** (`asyncio.Queue`):
- Single worker task drains queue sequentially
- Each item: `await asyncio.wait_for(agent.run(prompt), timeout=timeout_seconds)`
- On success: emit `schedule:completed`, update metadata
- On failure: emit `schedule:failed`, increment failure count, check circuit breaker
- On timeout: emit `schedule:failed` with reason `TIMEOUT`

**Circuit breaker**:
- Track consecutive failures per schedule
- After `max_consecutive_failures`: disable schedule, emit `schedule:circuit_open`
- Requires manual re-enable (operator edits JSON or agent calls `schedule_update`)

### 5. Tools (`arcagent/modules/scheduler/tools.py`)

4 `RegisteredTool` instances registered during `startup()`.

| Tool | Parameters | Returns |
|------|-----------|---------|
| `schedule_create` | `type`, `prompt`, `expression`/`at`/`every_seconds`, `active_hours?`, `timeout_seconds?` | Created `ScheduleEntry` as JSON |
| `schedule_list` | `enabled_only: bool = false` | Array of `ScheduleEntry` as JSON |
| `schedule_update` | `id`, plus any updatable fields | Updated `ScheduleEntry` as JSON |
| `schedule_cancel` | `id`, `delete: bool = false` | Confirmation message |

**Tool execute pattern** (matching existing codebase convention):
```python
async def _handle_create(
    self,
    type: str,
    prompt: str,
    expression: str | None = None,
    at: str | None = None,
    every_seconds: int | None = None,
    active_hours: dict | None = None,
    timeout_seconds: int = 300,
) -> str:
    # Validate prompt (SEC-01)
    # Validate type-specific fields
    # Check quota (max_schedules)
    # Create ScheduleEntry
    # store.add(entry)
    # emit schedule:created event
    # return entry.model_dump_json()
```

### 6. Module Integration (`arcagent/modules/scheduler/__init__.py`)

```python
class SchedulerModule:
    """Scheduling module — Module Bus participant."""

    def __init__(
        self,
        config: SchedulerConfig,      # DI: getattr(ctx.config, "scheduler")
        telemetry: Any,               # DI: ctx.telemetry
        workspace: Path,              # DI: ctx.workspace
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._store = ScheduleStore(workspace / "schedules.json")
        self._engine: SchedulerEngine | None = None

    @property
    def name(self) -> str:
        return "scheduler"

    async def startup(self, ctx: ModuleContext) -> None:
        # Register 4 tools
        # Subscribe to agent:shutdown
        # Create engine (needs reference to agent for agent.run())
        # Start engine timer loop

    async def shutdown(self) -> None:
        # Stop engine (drain queue)
        # Persist final state
```

**MODULE.yaml**:
```yaml
name: scheduler
version: 0.1.0
description: Agent self-scheduling with cron, interval, and one-time tasks
entry_point: arcagent.modules.scheduler:SchedulerModule
dependencies:
  - arcagent.core.module_bus
  - arcagent.core.config
events:
  subscribes:
    - agent:shutdown
  emits:
    - schedule:created
    - schedule:updated
    - schedule:cancelled
    - schedule:fired
    - schedule:completed
    - schedule:failed
    - schedule:skipped
    - schedule:circuit_open
```

### 7. CLI Command (`arccli/agent.py` addition)

```python
@agent.command("serve")
@click.argument("path")
@click.option("--verbose", is_flag=True)
def agent_serve(path: str, verbose: bool) -> None:
    """Run agent as daemon with active scheduler."""
    _load_env()
    agent_dir = _resolve_agent_dir(path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    loop.run_until_complete(_serve(agent_dir, shutdown_event, verbose))
    loop.close()
```

Uses manual event loop (NOT `asyncio.run()`) for graceful shutdown control per research findings.

### 8. Audit Events

All events emitted through Module Bus, flowing to existing OpenTelemetry pipeline.

| Event | Data | NIST AU-3 Fields |
|-------|------|------------------|
| `schedule:created` | entry, provenance | event_type, timestamp, agent_did, outcome |
| `schedule:fired` | schedule_id, fire_time | event_type, timestamp, agent_did, outcome |
| `schedule:completed` | schedule_id, duration, tokens, cost | event_type, timestamp, agent_did, outcome, tools_invoked |
| `schedule:failed` | schedule_id, error, duration | event_type, timestamp, agent_did, outcome, failure_reason |
| `schedule:skipped` | schedule_id, reason | event_type, timestamp, agent_did, outcome |
| `schedule:circuit_open` | schedule_id, failure_count | event_type, timestamp, agent_did, outcome |

## Security Design

### Prompt Validation (SEC-01)
- Max length: 500 chars (configurable)
- Injection pattern detection: `ignore previous`, `disregard`, `system:`, URLs, emails
- Provenance tagging on metadata

### Resource Exhaustion (SEC-02, SEC-03)
- Per-execution timeout via `asyncio.wait_for()`
- Circuit breaker: 3 consecutive failures → disable schedule
- Max 50 schedules per agent (configurable)
- Minimum 60-second interval (configurable)

### Audit Compliance (SEC-04)
- All 7 NIST AU-3 fields in every event
- Events flow through existing telemetry pipeline
- Structured logging for SIEM integration

## File Changes Summary

| File | Change |
|------|--------|
| `arcagent/core/config.py` | Add `SchedulerConfig`, add to `ArcAgentConfig` |
| `arcagent/modules/scheduler/__init__.py` | `SchedulerModule` class |
| `arcagent/modules/scheduler/models.py` | Pydantic models |
| `arcagent/modules/scheduler/store.py` | JSON file persistence |
| `arcagent/modules/scheduler/scheduler.py` | Timer loop + execution queue |
| `arcagent/modules/scheduler/tools.py` | 4 CRUD tools |
| `arcagent/modules/scheduler/MODULE.yaml` | Module manifest |
| `arccli/src/arccli/agent.py` | Add `arc agent serve` command |
| `tests/unit/modules/scheduler/` | Unit tests (frozen time) |
| `tests/integration/` | Integration test (mock LLM) |
