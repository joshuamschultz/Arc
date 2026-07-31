# PRD: ArcLLM Call Queue

**SPEC-014** | **Status**: PENDING | **Type**: Internal Module

## Problem Statement

ArcLLM fires every `invoke()` call immediately. When multiple modules (bio_memory, policy, scheduler) or multiple agents sharing the same endpoint make concurrent LLM calls, there is no concurrency management. Callers set timeouts that start at enqueue time, but calls sit invisibly waiting for provider capacity. The timeout burns while the call hasn't even been sent yet.

**Root cause observed**: Bio memory consolidation on mosa_agent silently failed — daily notes saved but no episodes or entities. The consolidation coroutine makes 4 sequential LLM calls to a reasoning model (30-60s each). `spawn_background` wraps the entire coroutine in a 120s timeout. The timeout includes invisible queue wait, killing the pipeline mid-execution.

## Goals

1. **Calls just work** — Module authors call `model.invoke()` and get a response. No thinking about timeouts, concurrency, or rate limits at the caller level.
2. **Send-time timeouts** — Timeouts measure actual LLM response time, not invisible queue wait.
3. **Bounded concurrency** — Configurable limit on simultaneous in-flight calls per model instance.
4. **Backpressure** — Clear rejection when too many calls are queued, preventing unbounded memory growth.
5. **Observability** — Queue depth, wait time, and rejection events visible in Otel traces and audit logs.

## Non-Goals

- Not a general-purpose job scheduler (arcrun handles that)
- Not cross-machine coordination (NATS handles multi-agent)
- Not smart routing or automatic model selection (caller picks the model)
- Not retry logic (existing RetryModule handles retries)
- No priority queuing (FIFO is sufficient; industry research confirms no major LLM framework implements priority)

## Requirements

### R-001: Concurrency Limiting
The QueueModule MUST limit the number of concurrent in-flight calls to a configurable `max_concurrent` value (default: 2). When the limit is reached, additional calls wait in FIFO order until a slot opens.

### R-002: Send-Time Timeout
The QueueModule MUST enforce a per-call timeout that starts when the call is dispatched to the inner module (semaphore acquired), NOT when the call is enqueued. Default: 60.0 seconds.

### R-003: Backpressure
The QueueModule MUST reject calls with `QueueFullError` when the number of waiting callers exceeds a configurable `max_queued` value (default: 10). This prevents unbounded memory growth.

### R-004: Otel Span Attributes
The QueueModule MUST set the following attributes on the current Otel span (if active):
- `arc.queue.wait_ms` (int) — time spent waiting for a semaphore slot
- `arc.queue.depth` (int) — number of waiters when this call entered the queue
- `arc.queue.rejected` (bool) — whether this call was rejected by backpressure
- `arc.queue.call_timeout_ms` (int) — configured call timeout

### R-005: Exception Hierarchy
Two new exceptions under `ArcLLMError`:
- `QueueFullError` — raised when backpressure rejects a call
- `QueueTimeoutError` — raised when a call exceeds the send-time timeout

### R-006: Configuration
The QueueModule MUST follow the standard `load_model()` kwarg pattern:
- `queue=True` — enable with config.toml defaults
- `queue=False` — disable
- `queue={...}` — enable with custom settings merged over defaults
- `queue=None` — use config.toml enabled flag

Config keys: `max_concurrent` (int), `call_timeout` (float, seconds), `max_queued` (int).

### R-007: Stack Position
The QueueModule MUST sit between OtelModule (outermost) and TelemetryModule in the module wrapping stack:
`Otel → Queue → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter`

### R-008: Audit Events (NIST 800-53 AU-2)
All queue state changes (enqueue, dequeue, send, timeout, reject) MUST be logged as structured audit events.

### R-009: Transparent API
The QueueModule MUST NOT change the `model.invoke()` API contract. Callers interact with the same `LLMProvider` interface. The queue is invisible.

## User Stories

### US-001: Bio Memory Consolidation
As a bio_memory module, I make 4 sequential eval calls during consolidation. With the QueueModule, each call's timeout starts when it actually fires, not when it's enqueued. My pipeline completes reliably even when the main loop is also using the LLM.

### US-002: Concurrent Module Eval
As multiple modules (policy + bio_memory) triggering eval calls on post_respond, the QueueModule ensures only `max_concurrent` calls hit the provider simultaneously. Calls are queued FIFO and dispatched as slots open.

### US-003: Overload Protection
As an agent receiving a burst of requests, when the queue is full (`max_queued` exceeded), new calls get `QueueFullError` immediately instead of accumulating unbounded work.

### US-004: Debugging Slow Calls
As an operator viewing Otel traces, I can distinguish "slow because of queue wait" vs "slow because of provider response" by checking `arc.queue.wait_ms` vs total span duration.

## Success Metrics

| Metric | Target |
|--------|--------|
| Bio memory consolidation success rate | 100% (currently ~50% due to timeouts) |
| Queue rejection rate | <5% under normal load |
| Module LOC | <100 lines (queue.py) |
| Test cases | 6 unit tests, all passing |
| API changes | 0 (transparent to callers) |

## Constraints

- Zero new dependencies (stdlib asyncio only)
- Per-process only (cross-machine is a NATS concern)
- Must not break existing `model.invoke()` callers
- Federal compliance: audit events on all queue operations (NIST AU-2, AU-9)
- Core LOC budget: module is NOT core (lives in modules/), so no budget impact
