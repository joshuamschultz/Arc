# scheduling-heartbeat — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-119–D-130 (12 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: Scheduling / Heartbeat / Cron MVP

**Date**: 2026-02-16
**Source Design**: `packages/arcagent/docs/arcagent-design-v3.md` Section 4
**Goal**: Build the scheduling system that lets agents manage their own recurring work

#### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|

#### Research Insights (via /deepen)

**Enriched**: 2026-02-16 | **Sources**: 3 parallel research agents (asyncio patterns, codebase analysis, security edge cases)

##### D1 — Module Architecture: Integration Patterns from Codebase

The existing module system has a precise DI contract. A scheduler module must follow:

1. **MODULE.yaml** at `arcagent/modules/scheduler/MODULE.yaml` — entry_point must start with `arcagent.modules.` (enforced at `module_loader.py:147`, ASI-04 protection)
2. **Constructor DI** — parameters must match the `available` dict in `module_loader.py:190-196`: `config`, `eval_config`, `llm_config`, `telemetry`, `workspace`
3. **Config lookup** — `getattr(ctx.config, manifest.name, None)` means `ArcAgentConfig.scheduler: SchedulerConfig` must exist as a field
4. **Module Protocol** — must satisfy `Module` Protocol: `name` property, `async startup(ctx)`, `async shutdown()`
5. **TOML enablement** — requires `[modules.scheduler]` with `enabled = true`

The memory module (`markdown_memory.py`) is the closest analog — same constructor pattern, same startup pattern (subscribe to bus events + register tools).

##### D3 — JSON Storage: Atomic Write Patterns

**Critical pattern**: `os.replace()` is atomic on all platforms. Temp file must be in same directory (same filesystem).

```python
fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, prefix=f".{filepath.name}.", suffix=".tmp")
os.write(fd, json_bytes)
os.fsync(fd)  # Force to disk before rename
os.close(fd)
os.replace(tmp_path, filepath)  # Atomic swap
```

**TOCTOU risk**: Read-modify-write on `schedules.json` is NOT atomic even with `os.replace()`. Since our scheduler is single-process sequential, this is safe for MVP. If we ever go multi-process, need `fcntl.flock()` or advisory locks.

**Edge case from Celery Beat**: Race condition between schedule sync and schedule update caused production crashes (django-celery-beat #158). Our sequential queue design avoids this.

##### D4 — Execution: agent.run() Interface

From codebase analysis, `agent.run(task)` returns `LoopResult`:
- `content: str | None` — final response
- `turns: int` — number of loop iterations
- `tool_calls_made: int` — total tool invocations
- `tokens_used: dict` — token breakdown
- `cost_usd: float` — dollar cost
- `events: list` — event history

This gives us everything needed for schedule metadata: `last_result`, `run_count`, cost tracking per execution.

##### D5 — Tools: Registration Pattern

Tools use `RegisteredTool` with `**kwargs` execute (NOT `(args, ctx)` like arcrun):

```python
RegisteredTool(
    name="schedule_create",
    description="Create a new schedule",
    input_schema={...},  # JSON Schema
    transport=ToolTransport.NATIVE,
    execute=self._handle_create,  # async **kwargs
)
ctx.tool_registry.register(tool)
```

Register during `startup()`, same as memory module's `_register_search_tool()`.

##### D6 — Constraints: Resource Exhaustion Guardrails

**Critical security finding**: Autonomous scheduled agents are a prime target for resource exhaustion attacks.

Recommended guardrails (from OWASP LLM10 + agentic security research):

| Guardrail | Threshold | Rationale |
|-----------|-----------|-----------|
| Max schedules per agent | 50 | Prevents schedule bombing |
| Minimum interval | 300s (5 min) | Prevents token drain |
| Prompt max length | 500 chars | Limits injection surface |
| Token budget per execution | 10,000 | Prevents runaway sessions |
| Execution timeout | 120s | Unattended must complete quickly |
| Loop detection window | 5 actions | Same tool+args 2x = kill |

**Circuit breaker**: After 3 consecutive failures, open circuit for 5 minutes. Emit `schedule:circuit_open` event.

##### D7 — Sequential Queue: asyncio.Queue Pattern

`asyncio.Queue` with `asyncio.wait_for()` is confirmed race-condition free by CPython maintainers. Pattern:

```python
item = await asyncio.wait_for(self.queue.get(), timeout=1.0)  # Won't lose items
# ... process ...
self.queue.task_done()  # Signals completion for queue.join()
```

Use `queue.join()` during graceful shutdown to drain pending work.

##### D8 — Daemon: Graceful Shutdown Pattern

**Do NOT use `asyncio.run()`** — it auto-cancels tasks without control. Instead:

```python
loop = asyncio.new_event_loop()
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, handle_signal)
loop.run_until_complete(startup())
loop.run_forever()  # Until shutdown
loop.run_until_complete(shutdown())
loop.close()
```

**Memory management**: Use `asyncio.BoundedSemaphore` to limit concurrent operations. Monitor with `psutil` health checks every 60s. Alert at 500MB.

**CLI pattern**: Click sync command wraps async function. Follows `arc agent run` pattern at `arccli/agent.py`.

##### D10 — Audit: NIST 800-53 AU-2/AU-3 Compliance

**AU-3 requires 7 fields** in every audit record:

1. **Event type** — `schedule.created`, `schedule.fired`, `schedule.completed`, `schedule.failed`
2. **Timestamp** — ISO 8601 with timezone (NTP-synced)
3. **Location** — component name + process ID
4. **Source** — agent DID + originating service
5. **Outcome** — SUCCESS, FAILURE, DENIED, ERROR
6. **Associated identities** — agent DID, tools invoked, human user (if applicable)
7. **Classification** — data sensitivity level

**Additional requirements**:
- Tamper-evident records (SHA-256 hash per record)
- SIEM integration for AU-6 automated analysis
- Alert on: any DENIED outcome, 3+ failures in 5 min, budget exceeded, circuit breaker trip
- 1-year retention minimum (federal), 7 years for sensitive systems

We already have OpenTelemetry + `AgentTelemetry` — emit structured events through existing pipeline.

##### D11 — croniter: DST Gotcha

**Critical bug**: croniter computes wrong next execution during DST transitions. Fix:

1. Always use `pytz.timezone(tz_name).localize()` — never `datetime(tzinfo=tz)`
2. After any timedelta math, call `tz.normalize()` to handle DST transitions
3. Use tolerance window (30-60s) for scheduler drift
4. Store all times in UTC, convert to local TZ only for cron evaluation

##### Security: Prompt Injection via Schedules

**Three-layer defense** (from OWASP AI Agent Security Cheat Sheet):

1. **Syntactic**: Length limits, character allowlist `[a-zA-Z0-9\s\-_.,;:]`, format validation
2. **Semantic**: Detect injection patterns (`ignore previous`, `disregard`, `system:`, URLs, email addresses)
3. **Provenance**: Tag origin (`user-created` vs `agent-generated` vs `external-data`), require human approval for external-data schedules

Log original unsanitized input separately from sanitized version for forensics.

##### Security: Execution Deduplication

Each schedule fire should generate a unique execution ID (UUID). Pattern from AWS EventBridge:
- `execution_id = f"{schedule_id}:{fire_timestamp_unix}"`
- Check dedup table before executing
- Prevents double-fires if scheduler restarts mid-cycle

#### Key Design Principles

- **Agent self-schedules**: The agent writes both the WHEN and the WHAT. It creates cron/interval/once entries with the prompt describing what to do.
- **Module, not core**: Scheduling is opt-in. Module Bus participant. Doesn't bloat the nucleus.
- **Daemon model**: `arc agent serve` runs the scheduler as a long-running process. Agent stays warm. Each schedule fire is an independent `agent.run(prompt)` with a fresh session.
- **Sequential queue**: Overlapping fires queue up. No concurrent executions. FIFO order.
- **Zero infrastructure**: JSON file storage. No database. No message queue. Just files and asyncio.

#### Components to Build

1. **`arcagent/modules/scheduler/`** — Module with MODULE.yaml
   - `scheduler.py` — Core scheduler engine (asyncio timer loop, cron evaluation, queue)
   - `models.py` — Pydantic models (ScheduleEntry, ScheduleMetadata, ActiveHours)
   - `store.py` — JSON file persistence (load/save with atomic writes)
   - `tools.py` — 4 agent-facing tools (create/list/update/cancel)

2. **`arcagent/core/config.py`** — Add SchedulerConfig to ArcAgentConfig

3. **`arccli`** — Add `arc agent serve` command

4. **Tests**
   - `tests/unit/modules/scheduler/` — Frozen-time unit tests
   - `tests/integration/` — End-to-end with mock LLM

#### Architecture Diagram

```
arc agent serve
    |
    v
ArcAgent (warm, long-running)
    |
    v
SchedulerModule (Module Bus)
    |
    +-- loads workspace/schedules.json
    +-- evaluates timing (croniter for cron, timedelta for interval, datetime for once)
    +-- checks active_hours
    |
    v (when schedule fires)
    |
    +-- emit schedule:fired event
    +-- queue execution
    +-- agent.run(prompt) with fresh session
    +-- emit schedule:completed/failed event
    +-- update metadata (last_run, last_result, run_count)
    +-- persist to schedules.json
```

---

---
