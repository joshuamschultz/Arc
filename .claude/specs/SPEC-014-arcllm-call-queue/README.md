# SPEC-014: ArcLLM Call Queue

| Field | Value |
|-------|-------|
| ID | SPEC-014 |
| Feature | ArcLLM Call Queue (QueueModule) |
| Type | Internal module (no API, no UI) |
| Status | COMPLETE |
| Created | 2026-02-27 |
| Completed | 2026-03-01 |
| Package | arcllm |
| Priority | High — root cause of bio_memory consolidation failures |

## Prior Work

| Phase | Location | Status |
|-------|----------|--------|
| Brainstorm | `.claude/brainstorms/2026-02-27-arcllm-call-queue.md` | Complete |
| Build Decisions | `.claude/decisions-log.md` (section: "ArcLLM Call Queue") | Complete (10 decisions) |
| Deepen Research | Embedded in decisions-log.md as "Research Insights" | Complete (4 agents) |

## Key Decisions

| # | Decision | Summary |
|---|----------|---------|
| D-010 | Stack position | Between OtelModule and TelemetryModule |
| D-011 | Queue scope | Per adapter instance |
| D-012 | Concurrency primitive | asyncio.BoundedSemaphore |
| D-013 | Backpressure | Max waiters limit, QueueFullError on overflow |
| D-014 | Timeout semantics | Send-time only (starts after semaphore acquired) |
| D-015 | Configuration | Standard load_model() kwarg pattern |
| D-016 | Exception hierarchy | QueueFullError + QueueTimeoutError under ArcLLMError |
| D-020 | Telemetry | arc.queue.* Otel span attributes on existing span |
| D-030 | Default concurrency | max_concurrent=2 |
| D-031 | Default timeout | call_timeout=60.0s |
| D-040 | Testing | 6 unit tests with mock inner adapter |

## Solutions Referenced

- `.claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md` — unbounded queue flagged as medium-severity (#7)

## Learnings

### Implementation

- `asyncio.BoundedSemaphore` is the correct primitive — prevents over-release bugs that `Semaphore` allows
- `_waiters` counter is safe without locks because asyncio event loop is single-threaded (no atomicity concerns)
- `async with self._semaphore:` context manager ensures release on all exit paths including `CancelledError`
- Backpressure check (`_waiters >= _max_queued`) must happen BEFORE incrementing `_waiters` to prevent a waiter from rejecting itself

### Testing

- Testing concurrent asyncio code requires careful timing: `asyncio.sleep(0.05)` between task creation to let the event loop schedule them
- `max_queued=0` means "no waiters allowed" — the first check `0 >= 0` is True, so even the first overflow call is rejected. Tests need `max_queued >= 1` to allow at least one waiter before triggering backpressure
- Otel span attribute verification works well with `MagicMock` + `patch("...trace.get_current_span")`

### Architecture

- QueueModule follows the established `BaseModule` pattern exactly — config dict + inner provider, `validate_config_keys`, override `invoke()`
- No module-level state means `clear_cache()` needs no queue-specific cleanup
- Stack position between Otel and Telemetry is correct: Otel creates the span, Queue adds attributes to it, Telemetry tracks tokens/cost (unaffected by queue)
- 72 LOC for queue.py (well under the 100 LOC target from PRD)

## Files Changed

| File | Change | LOC |
|------|--------|-----|
| `src/arcllm/modules/queue.py` | NEW | 72 |
| `src/arcllm/exceptions.py` | MODIFY | +25 |
| `src/arcllm/registry.py` | MODIFY | +8 |
| `src/arcllm/config.toml` | MODIFY | +5 |
| `src/arcllm/modules/__init__.py` | MODIFY | +2 |
| `src/arcllm/__init__.py` | MODIFY | +5 |
| `tests/test_queue.py` | NEW | ~200 |
| `tests/test_registry.py` | MODIFY | +40 |
