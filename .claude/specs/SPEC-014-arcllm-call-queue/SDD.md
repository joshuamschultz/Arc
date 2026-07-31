# SDD: ArcLLM Call Queue

**SPEC-014** | **Status**: PENDING

## Architecture Overview

The QueueModule is a new arcllm module that wraps an inner `LLMProvider` and gates `invoke()` calls through an `asyncio.BoundedSemaphore`. It sits between OtelModule and TelemetryModule in the wrapping stack.

```
Caller → model.invoke()
   ↓
OtelModule        ← creates span, records timing
   ↓
QueueModule       ← enforces concurrency, sets span attributes
   ↓
TelemetryModule   ← tracks tokens, cost, budget
   ↓
AuditModule       ← logs PII-safe audit events
   ↓
SecurityModule    ← PII redaction, signing
   ↓
RetryModule       ← exponential backoff on transient failures
   ↓
FallbackModule    ← provider failover chain
   ↓
RateLimitModule   ← token bucket per endpoint
   ↓
Adapter           ← HTTP call to provider
```

## Component Design

### C-001: QueueModule (`modules/queue.py`)

**Extends**: `BaseModule`

**State** (per instance, no shared globals):
- `_semaphore: asyncio.BoundedSemaphore` — gates concurrent calls
- `_waiters: int` — current count of calls waiting for semaphore
- `_max_queued: int` — backpressure limit
- `_call_timeout: float` — send-time timeout in seconds

**invoke() flow**:
```
1. Check backpressure: if _waiters >= _max_queued → raise QueueFullError
2. Increment _waiters
3. Record queue entry time
4. Acquire semaphore (FIFO wait)
5. Decrement _waiters
6. Record queue wait time
7. Set Otel span attributes (arc.queue.wait_ms, arc.queue.depth, etc.)
8. Execute inner.invoke() with asyncio.wait_for(timeout=_call_timeout)
9. Return response (or raise QueueTimeoutError on timeout)
```

**Config keys**:
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_concurrent` | int | 2 | Semaphore capacity |
| `call_timeout` | float | 60.0 | Send-time timeout (seconds) |
| `max_queued` | int | 10 | Max waiters before rejection |

**Valid config keys** (for `validate_config_keys`): `{"enabled", "max_concurrent", "call_timeout", "max_queued"}`

### C-002: Exceptions (`exceptions.py`)

Two new exceptions appended to existing file:

```python
class QueueFullError(ArcLLMError):
    """Raised when queue backpressure rejects a call."""
    def __init__(self, current_waiters: int, max_queued: int) -> None:
        self.current_waiters = current_waiters
        self.max_queued = max_queued
        super().__init__(
            f"Queue full: {current_waiters} calls waiting (max {max_queued})"
        )

class QueueTimeoutError(ArcLLMError):
    """Raised when a call exceeds the send-time timeout."""
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"LLM call timed out after {timeout:.1f}s (send-time)")
```

### C-003: Registry Integration (`registry.py`)

Add `queue` kwarg to `load_model()` signature and wire into the module stack between Otel and Telemetry.

**Changes**:
1. Add `queue: bool | dict[str, Any] | None = None` parameter to `load_model()`
2. Insert queue module wrapping after telemetry, before otel in the wrapping sequence:

```python
# After telemetry_config block, before otel_config block:
queue_config = _resolve_module_config("queue", queue)
if queue_config is not None:
    from arcllm.modules.queue import QueueModule
    result = QueueModule(queue_config, result)
```

3. Update `load_model()` docstring to document the new kwarg and updated stack order.

### C-004: Global Config (`config.toml`)

Add default queue module config:

```toml
[modules.queue]
enabled = false
max_concurrent = 2
call_timeout = 60.0
max_queued = 10
```

### C-005: Module Package (`modules/__init__.py`)

Add `QueueModule` to imports and `__all__`.

### C-006: Otel Integration

The QueueModule does NOT create its own span. Instead, it adds attributes to the active span created by OtelModule (which wraps it):

```python
from opentelemetry import trace

span = trace.get_current_span()
if span.is_recording():
    span.set_attribute("arc.queue.wait_ms", wait_ms)
    span.set_attribute("arc.queue.depth", depth_at_entry)
    span.set_attribute("arc.queue.call_timeout_ms", int(self._call_timeout * 1000))
```

On rejection, set `arc.queue.rejected = True` before raising `QueueFullError`.

## Concurrency Safety

- **BoundedSemaphore** (not Semaphore) prevents over-release bugs from unbalanced acquire/release
- `async with self._semaphore:` context manager ensures release on all exit paths including `CancelledError`
- `_waiters` counter uses simple increment/decrement (single asyncio event loop = single-threaded, no atomicity concerns)
- Per-instance state means zero shared mutable state between model instances

## Error Handling

| Error | Cause | Caller Impact |
|-------|-------|---------------|
| `QueueFullError` | `_waiters >= _max_queued` | Immediate rejection, caller decides (skip/retry) |
| `QueueTimeoutError` | `asyncio.wait_for()` timeout | Call was sent but response too slow |
| `CancelledError` | External cancellation | Semaphore released via context manager, propagates |
| Inner exceptions | Provider errors | Pass through unchanged (retry module handles) |

## Dependencies

- **New**: None (stdlib `asyncio` only)
- **Existing**: `opentelemetry.trace` (already a dependency via base.py)

## File Impact Summary

| File | Change | LOC Impact |
|------|--------|------------|
| `modules/queue.py` | NEW | ~70-90 lines |
| `exceptions.py` | MODIFY | +20 lines |
| `registry.py` | MODIFY | +10 lines |
| `config.toml` | MODIFY | +5 lines |
| `modules/__init__.py` | MODIFY | +2 lines |
| `tests/test_queue.py` | NEW | ~150-200 lines |
| **Total** | | ~260-330 lines |
