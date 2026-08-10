# arcllm-call-queue — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-277–D-290 (14 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcLLM Call Queue — Build Decisions (2026-02-27)

**Phase**: build | **Status**: complete | **Total decisions**: 10 (7 user, 3 auto-applied)
**Priority framework**: simplicity → security → scalability → compliance
**Brainstorm**: `.claude/brainstorms/2026-02-27-arcllm-call-queue.md`

#### Summary

An internal QueueModule for arcllm that manages LLM call concurrency with per-call timeouts that start at **send time**, not enqueue time. Sits between OtelModule and TelemetryModule in the wrapping stack. Uses asyncio.Semaphore for concurrency control, bounded waiters for backpressure, and piggybacks on Otel spans for observability. Configured via the standard `load_model()` kwarg pattern.

#### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
















#### Research Insights (via /deepen — 2026-02-27)

**Enhancement summary**: 4 parallel research agents investigated asyncio.Semaphore edge cases, LLM SDK queue patterns, Otel span attribute conventions, and arcllm module wrapping patterns. Key findings below. No decision changes required — all findings reinforce or refine existing decisions.




##### Module Implementation — arcllm Wrapping Pattern

- **BaseModule interface confirmed**: Extend `BaseModule(config, inner)`, override `async def invoke(self, messages, **kwargs)`, delegate via `await self._inner.invoke(messages, **kwargs)`.
- **`_span()` context manager**: Available from BaseModule for creating sub-spans. Not needed here since we piggyback on OtelModule's span, but available if we add queue-specific spans later.
- **`load_model()` integration**: Add `queue` to `_MODULE_DEFAULTS` dict in `registry.py`. The `_resolve_module_config()` function handles `True|False|dict|None` automatically. Module instantiation order in `_wrap_with_modules()` determines stack position.
- **Config**: Add `[modules.queue]` section to provider TOML files with `max_concurrent`, `call_timeout`, `max_queued` defaults. Config class inherits from `BaseModuleConfig`.

**Implementation checklist** (from codebase analysis):
1. `modules/queue.py` — QueueModule class extending BaseModule
2. `modules/config.py` — add QueueConfig (max_concurrent, call_timeout, max_queued)
3. `exceptions.py` — add QueueFullError, QueueTimeoutError under ArcLLMError
4. `registry.py` — add `queue` to `_MODULE_DEFAULTS`, wire into `_wrap_with_modules()` at correct stack position
5. Provider TOML files — add `[modules.queue]` default config
6. Tests — 6 test cases per D-290

#### Categories Skipped (Not Applicable)

- **Data Model**: No persistence — pure in-memory asyncio state
- **API Design**: No endpoints — internal module only
- **Security**: Beyond auto-applied mandates, no additional security decisions — queue doesn't handle secrets, credentials, or user data
- **Integration**: No external services — queue is between caller and adapter
- **Extensibility**: Standard module pattern, no additional extension points needed
- **Deployment**: No migration — new module, additive change
- **UI/UX**: No user interface

#### Open Questions

None — all resolved during build and deepen.

---

## Per-decision deepen notes

### D-282 — Research Insights (via /deepen — 2026-02-27)


- **Use `BoundedSemaphore` over `Semaphore`**: `BoundedSemaphore` raises `ValueError` if released more times than acquired. Prevents over-release bugs from unbalanced acquire/release in error paths. Negligible overhead.
- **FIFO guarantee**: Python 3.11+ guarantees FIFO ordering on `asyncio.Semaphore` (CPython implementation uses `collections.deque`). Earlier versions had edge cases but were functionally FIFO. Our min Python target (3.11+) is safe.
- **Cancellation safety**: `async with semaphore:` is safe — `__aexit__` always calls `release()`, even on `asyncio.CancelledError`. No manual try/finally needed when using context manager pattern.
- **Implementation note**: Track `_waiters` count with a simple `int` counter (increment on enter wait, decrement on acquire or reject). Don't introspect semaphore internals.

### D-283 — Research Insights (via /deepen — 2026-02-27)


- **No major LLM framework implements priority queuing** — LiteLLM, LangChain, OpenAI SDK all use simple FIFO with concurrency limits. This validates our decision to start with FIFO and skip priority levels.
- **LiteLLM pattern**: Uses `asyncio.Semaphore(max_parallel_requests)` with a simple `num_retries` fallback. No explicit backpressure — callers just wait. Our `max_queued` adds the missing safety valve.
- **Reject-fast is correct**: Industry pattern is to fail fast with clear error when queue is full, rather than silent eviction or unbounded waiting. `QueueFullError` matches this.

### D-287 — Research Insights (via /deepen — 2026-02-27)


- **No standard OTel semantic convention for queue wait time** — there's no `rpc.queue.wait` or `messaging.queue.depth` attribute in the OTel semantic conventions spec. Custom attributes are the correct approach.
- **Naming convention**: Use `arc.queue.` prefix for all custom attributes (e.g., `arc.queue.wait_ms`, `arc.queue.depth`, `arc.queue.rejected`). This follows OTel's [attribute naming guidelines](https://opentelemetry.io/docs/specs/semconv/general/attribute-naming/) for vendor-specific attributes.
- **Access pattern**: Use `trace.get_current_span()` from within QueueModule to add attributes to the outer OtelModule's active span. This avoids creating a new span and keeps the trace structure clean.
- **Attribute types**: `wait_ms` as `int`, `depth` as `int`, `rejected` as `bool`, `call_timeout_ms` as `int`. OTel prefers integer milliseconds over float seconds for span attributes.

---
