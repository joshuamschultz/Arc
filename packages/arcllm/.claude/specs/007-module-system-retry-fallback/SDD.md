# SDD: Module System + Retry + Fallback

> System design for ArcLLM Step 7.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 7 establishes the module system pattern and delivers the first two modules. The key insight: modules are wrapper classes that implement the same `LLMProvider` interface as adapters. This means they're transparent — the agent calls `model.invoke()` without knowing whether it's talking to a raw adapter or an adapter wrapped in 5 modules.

Design priorities:
1. **Composability** — modules stack in any order, each independent
2. **Transparency** — same interface as adapter, agent code unchanged
3. **Testability** — each module testable in isolation with a mock inner provider
4. **Config-driven** — activation via config.toml or load-time kwargs

---

## Directory Map

```
src/arcllm/
├── modules/
│   ├── __init__.py                    # NEW: Module exports
│   ├── base.py                        # NEW: BaseModule wrapper class
│   ├── retry.py                       # NEW: RetryModule
│   └── fallback.py                    # NEW: FallbackModule
├── registry.py                        # MODIFY: Module stacking logic
├── __init__.py                        # MODIFY: Export new module types
├── exceptions.py                      # MODIFY: Add ArcLLMRetryError (optional)
tests/
├── test_module_base.py                # NEW: BaseModule tests
├── test_retry.py                      # NEW: RetryModule tests
├── test_fallback.py                   # NEW: FallbackModule tests
├── test_registry.py                   # MODIFY: Add module integration tests
```

---

## Component Design

### 1. BaseModule (`modules/base.py`)

The foundation class for all modules. Implements `LLMProvider` by delegating to an inner provider.

| Attribute/Method | Purpose |
|-----------------|---------|
| `inner: LLMProvider` | The wrapped provider (adapter or another module) |
| `name: str` | Delegated from inner (transparent) |
| `invoke(messages, tools, **kwargs)` | Default: delegates to `inner.invoke()`. Subclasses override. |
| `validate_config()` | Delegated from inner |

Key design: `BaseModule` is NOT abstract. Its default `invoke()` delegates straight through. Subclasses override to add behavior. This means you can test the base class directly.

### 2. RetryModule (`modules/retry.py`)

Wraps `invoke()` with retry logic for transient failures.

#### Configuration

From `config.toml [modules.retry]`:
```toml
[modules.retry]
enabled = false
max_retries = 3
backoff_base_seconds = 1.0
max_wait_seconds = 60.0
retryable_status_codes = [429, 500, 502, 503, 529]
```

Pydantic config model:
```
RetryConfig:
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    max_wait_seconds: float = 60.0
    retryable_status_codes: list[int] = [429, 500, 502, 503, 529]
```

#### Logic Flow

```
invoke(messages, tools, **kwargs):
    last_error = None
    for attempt in range(max_retries + 1):    # attempt 0 = first try
        try:
            return await inner.invoke(messages, tools, **kwargs)
        except retryable_error as e:
            last_error = e
            if attempt < max_retries:
                wait = min(base * 2^attempt + jitter, max_wait)
                await asyncio.sleep(wait)
    raise last_error
```

#### Retryable Error Detection

An error is retryable if:
- `ArcLLMAPIError` with `status_code` in `retryable_status_codes`
- `httpx.ConnectError` (network failure)
- `httpx.TimeoutException` (request timeout)

All other exceptions pass through immediately (not caught by retry).

#### Jitter

```python
jitter = random.uniform(0, backoff_base_seconds)
```

Random component added to each wait to desynchronize retries across agents.

#### Retry-After Header (P2)

When `ArcLLMAPIError` includes a `retry_after` value (from HTTP `Retry-After` header), use it as the minimum wait time instead of the calculated backoff. This requires:
- `ArcLLMAPIError` gains optional `retry_after: float | None` field
- Adapters extract `Retry-After` header and attach to error

### 3. FallbackModule (`modules/fallback.py`)

Catches exceptions from inner and tries the next provider in a config-driven chain.

#### Configuration

From `config.toml [modules.fallback]`:
```toml
[modules.fallback]
enabled = false
chain = ["anthropic", "openai"]
```

Pydantic config model:
```
FallbackConfig:
    chain: list[str]              # Provider names in priority order
```

#### Logic Flow

```
invoke(messages, tools, **kwargs):
    try:
        return await inner.invoke(messages, tools, **kwargs)
    except Exception as primary_error:
        # Inner failed — try fallback chain
        for provider_name in chain:
            try:
                fallback = load_model(provider_name)
                return await fallback.invoke(messages, tools, **kwargs)
            except Exception:
                continue  # Try next in chain
        # All fallbacks failed
        raise primary_error  # Re-raise original error
```

Key decisions:
- **On-demand creation**: Fallback adapters created via `load_model()` at failure time, not pre-loaded
- **Original error preserved**: If all fallbacks fail, the original (primary) error is raised, not the last fallback error
- **No retry on fallback**: Fallback providers are tried once. If retry is also enabled, it wraps fallback, so retries happen on each attempt

#### Stacking Order

When both retry and fallback are enabled:
```
RetryModule(FallbackModule(adapter))
```

This means: if the adapter fails, fallback tries the next provider. If that also fails, retry catches the error and retries the whole fallback chain. This gives maximum resilience.

### 4. Registry Integration (`registry.py` changes)

`load_model()` gains module stacking logic. After constructing the adapter, it checks for enabled modules and wraps them around the adapter.

#### Module Resolution

```python
def load_model(provider, model=None, **kwargs):
    # 1. Build adapter (existing logic)
    adapter = adapter_class(config, model_name)

    # 2. Load global config for module settings
    global_config = load_global_config()  # cached

    # 3. Stack enabled modules (inside-out order)
    wrapped = adapter

    # Fallback (inner layer)
    fallback_config = _resolve_module_config("fallback", global_config, kwargs)
    if fallback_config and fallback_config.get("enabled", False):
        wrapped = FallbackModule(fallback_config, wrapped)

    # Retry (outer layer)
    retry_config = _resolve_module_config("retry", global_config, kwargs)
    if retry_config and retry_config.get("enabled", False):
        wrapped = RetryModule(retry_config, wrapped)

    return wrapped
```

#### Config Resolution

Module settings merge from two sources:
1. `config.toml` — `[modules.retry]` section
2. `load_model()` kwargs — `retry=True` or `retry={"max_retries": 5}`

Resolution rules:
- `retry=True` → enable with config.toml defaults
- `retry=False` → disable even if config.toml says enabled
- `retry={"max_retries": 5}` → enable with merged settings (kwarg overrides)
- No kwarg → use config.toml as-is

### 5. Exception Changes

`ArcLLMAPIError` gains an optional `status_code` field so RetryModule can check it:

```python
class ArcLLMAPIError(ArcLLMError):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after  # P2: from Retry-After header
```

Check: Does `ArcLLMAPIError` already have `status_code`? If so, no change needed.

---

## ADRs

### ADR-018: Wrapper Module Pattern

**Context**: Modules need to intercept the invoke() call path. Options: wrapper classes, if-checks in invoke(), decorators, event hooks, subclass mixins.

**Decision**: Wrapper classes implementing `LLMProvider`. Each module is a class with its own `invoke()` that calls `inner.invoke()`.

**Rationale**: Composable (stack any order), testable (mock inner), single-responsibility (one concern per file), transparent (same interface as adapter). Scales to 7+ modules without invoke() becoming a conditional mess. ~30 lines of infrastructure (BaseModule) enables clean separation for all future modules.

**Alternatives rejected**:
- If-checks in invoke() — works for 1-2 modules but becomes unmaintainable at 7+
- Decorators — hard to configure per-instance, confusing bottom-up stacking
- Event hooks — scattered logic, hard to reason about execution order
- Subclass mixins — diamond problem, tight coupling to inheritance

### ADR-019: Separate Retry and Fallback Modules

**Context**: Retry and fallback are related (both handle failures) but distinct concerns.

**Decision**: Two separate modules. RetryModule handles retries within a provider. FallbackModule handles switching across providers.

**Rationale**: Independently composable. Some agents want retry without fallback (single provider). Some want fallback without retry (fast failover). Each file stays under 60 lines.

### ADR-020: Config-Driven Fallback Chain

**Context**: Fallback needs a list of alternative providers. Options: pre-loaded adapter list, config-driven with on-demand creation.

**Decision**: Chain defined in config.toml, adapters created via `load_model()` at failure time.

**Rationale**: No wasted memory creating adapters for providers that may never be needed. No requirement for API keys of all providers upfront. Leverages existing load_model() infrastructure.

### ADR-021: Exponential Backoff with Jitter

**Context**: Thousands of agents hitting a rate limit simultaneously will retry at the same time without jitter.

**Decision**: Wait time = `min(base * 2^attempt + uniform(0, base), max_wait)`.

**Rationale**: Exponential backoff reduces load over time. Jitter desynchronizes retries across agents. Standard approach for distributed systems (AWS, Google Cloud, Anthropic all recommend this).

---

## Edge Cases

| Case | Handling |
|------|----------|
| All retries exhausted | Raise original exception (last error) |
| Non-retryable error (401, 403) | Pass through immediately, no retry |
| All fallback providers fail | Raise the primary (first) error |
| Fallback chain is empty | FallbackModule passes through (no fallback) |
| Retry + Fallback both enabled | Retry wraps Fallback: retry the fallback chain |
| Module enabled in config but kwarg says `False` | Kwarg wins — module disabled |
| Module not in config but kwarg enables it | Module enabled with defaults |
| Connection timeout (no status code) | Retryable — `httpx.TimeoutException` caught |
| Module wraps module wraps adapter | Works — same interface throughout |
| `load_model()` called with no modules | Adapter returned directly (no wrapping) |

---

## Test Strategy

Three new test files, fully mocked (no real API calls).

| File | Tests | Priority |
|------|-------|----------|
| `test_module_base.py` | BaseModule delegation, interface compliance | P0 |
| `test_retry.py` | Retry logic, backoff, jitter, error detection | P0 |
| `test_fallback.py` | Chain walking, on-demand creation, error preservation | P0 |
| `test_registry.py` (additions) | Module stacking integration | P0 |

### Key Test Scenarios

**BaseModule:**
- Delegates invoke() to inner
- Delegates name and validate_config() to inner
- Works as transparent wrapper

**RetryModule:**
- Retries on 429 status code
- Retries on 500/502/503/529
- Retries on connection error
- Does NOT retry on 400/401/403
- Respects max_retries limit
- Backoff increases exponentially
- Raises last error after all retries
- First try succeeds — no retry
- Configurable retry codes

**FallbackModule:**
- Primary succeeds — no fallback triggered
- Primary fails, first fallback succeeds
- Primary fails, all fallbacks fail — raises primary error
- Empty chain — passes through
- Fallback adapter created via load_model()

**Registry Integration:**
- `load_model("anthropic", retry=True)` wraps with RetryModule
- `load_model("anthropic")` with config enabled wraps automatically
- `load_model("anthropic", retry=False)` disables even if config enabled
- Module stacking order correct (Retry outside Fallback)
