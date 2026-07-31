# SDD: Rate Limiter Module

> System design for ArcLLM Step 8.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 8 adds the second module to the system: a token bucket rate limiter with per-provider shared state. It validates module composability — after retry and fallback, this is the third module using the BaseModule wrapper pattern established in Step 7.

Key design insight: rate limiting is different from retry/fallback because it requires **shared state** across multiple `load_model()` instances. All agents using the same provider must share one bucket. This introduces the first shared-state pattern in the module system.

Design priorities:
1. **Correctness** — token bucket math is well-defined, implement exactly
2. **Async safety** — `asyncio.Lock` protects shared bucket from concurrent access
3. **Transparency** — agents don't know about rate limiting; calls just take longer
4. **Observability** — WARNING logs when throttled, so ops knows why calls are slow

---

## Directory Map

```
src/arcllm/
├── modules/
│   ├── __init__.py                    # MODIFY: Add RateLimitModule export
│   ├── base.py                        # UNCHANGED
│   ├── retry.py                       # UNCHANGED
│   ├── fallback.py                    # UNCHANGED
│   └── rate_limit.py                  # NEW: TokenBucket + RateLimitModule
├── registry.py                        # MODIFY: Add rate_limit= kwarg, call clear_buckets()
├── __init__.py                        # MODIFY: Add RateLimitModule to lazy imports
├── config.toml                        # MODIFY: Add burst_capacity to [modules.rate_limit]
tests/
├── test_rate_limit.py                 # NEW: Full test suite
├── test_registry.py                   # MODIFY: Add rate_limit stacking tests
```

---

## Component Design

### 1. TokenBucket (`modules/rate_limit.py`)

A standalone class implementing the token bucket algorithm. Not a module itself — used by `RateLimitModule`.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `_capacity` | `int` | Maximum tokens (burst size) |
| `_tokens` | `float` | Current available tokens |
| `_refill_rate` | `float` | Tokens added per second (`RPM / 60`) |
| `_last_refill` | `float` | `time.monotonic()` timestamp of last refill |
| `_lock` | `asyncio.Lock` | Async-safe concurrent access |

| Method | Purpose |
|--------|---------|
| `__init__(capacity, refill_rate)` | Initialize bucket with full tokens |
| `async acquire() -> float` | Consume one token, wait if empty. Returns wait time in seconds. |
| `_refill()` | Add tokens based on elapsed time, cap at capacity |

#### Token Bucket Algorithm

```
acquire():
    async with _lock:
        _refill()                              # Add tokens for elapsed time
        if _tokens >= 1.0:
            _tokens -= 1.0
            return 0.0                          # No wait
        # Calculate wait for next token
        deficit = 1.0 - _tokens
        wait_seconds = deficit / _refill_rate
        # Release lock during sleep (other callers can enter)
    await asyncio.sleep(wait_seconds)           # Sleep outside lock
    # Re-acquire after sleep
    async with _lock:
        _refill()
        _tokens -= 1.0                         # Should have a token now
        return wait_seconds

_refill():
    now = time.monotonic()
    elapsed = now - _last_refill
    _tokens = min(_capacity, _tokens + elapsed * _refill_rate)
    _last_refill = now
```

Key design choice: **sleep happens outside the lock**. This is critical — if we held the lock during sleep, all concurrent callers would serialize on the lock, queuing up. By releasing the lock, other callers can also compute their wait time and sleep concurrently.

### 2. RateLimitModule (`modules/rate_limit.py`)

Wraps an inner `LLMProvider` and acquires a token from the shared per-provider bucket before each `invoke()`.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `_bucket` | `TokenBucket` | Shared bucket for this provider |
| `_provider_name` | `str` | Provider name (for logging) |

#### Configuration

From `config.toml [modules.rate_limit]`:
```toml
[modules.rate_limit]
enabled = false
requests_per_minute = 60
burst_capacity = 60
```

Config keys:
- `requests_per_minute` (int, default 60): Sustained request rate. Becomes refill rate: `RPM / 60` tokens/sec.
- `burst_capacity` (int, default = requests_per_minute): Maximum bucket size. Controls how many requests can fire simultaneously before throttling.

#### Logic Flow

```
__init__(config, inner):
    validate: requests_per_minute > 0, burst_capacity >= 1
    rpm = config["requests_per_minute"]
    capacity = config.get("burst_capacity", rpm)
    provider_name = inner.name
    bucket = _get_or_create_bucket(provider_name, capacity, rpm / 60.0)
    store _bucket, _provider_name

invoke(messages, tools, **kwargs):
    wait = await _bucket.acquire()
    if wait > 0:
        logger.warning(
            "Rate limited for provider '%s'. Waited %.2fs for token.",
            _provider_name, wait
        )
    return await inner.invoke(messages, tools, **kwargs)
```

### 3. Shared Bucket Registry (`modules/rate_limit.py`)

Module-level dict mapping provider names to `TokenBucket` instances.

```python
_bucket_registry: dict[str, TokenBucket] = {}

def _get_or_create_bucket(provider: str, capacity: int, refill_rate: float) -> TokenBucket:
    if provider not in _bucket_registry:
        _bucket_registry[provider] = TokenBucket(capacity, refill_rate)
    return _bucket_registry[provider]

def clear_buckets() -> None:
    _bucket_registry.clear()
```

Note: The first `load_model()` call for a provider sets the bucket parameters. Subsequent calls for the same provider share the existing bucket (even if they pass different config). This is intentional — one bucket per provider, configured by the first user.

### 4. Registry Integration (`registry.py` changes)

#### load_model() signature change

```python
def load_model(
    provider: str,
    model: str | None = None,
    *,
    retry: bool | dict | None = None,
    fallback: bool | dict | None = None,
    rate_limit: bool | dict | None = None,  # NEW
) -> LLMProvider:
```

#### Stacking order change

Current: `Retry(Fallback(adapter))`
New: `Retry(Fallback(RateLimit(adapter)))`

Rate limit wraps the adapter first (innermost), then fallback, then retry outermost.

```python
# Stacking order (innermost first):
# 1. Rate limit (closest to adapter)
rate_limit_config = _resolve_module_config("rate_limit", rate_limit)
if rate_limit_config is not None:
    from arcllm.modules.rate_limit import RateLimitModule
    result = RateLimitModule(rate_limit_config, result)

# 2. Fallback
fallback_config = _resolve_module_config("fallback", fallback)
if fallback_config is not None:
    from arcllm.modules.fallback import FallbackModule
    result = FallbackModule(fallback_config, result)

# 3. Retry (outermost)
retry_config = _resolve_module_config("retry", retry)
if retry_config is not None:
    from arcllm.modules.retry import RetryModule
    result = RetryModule(retry_config, result)
```

#### clear_cache() update

```python
def clear_cache() -> None:
    # ... existing cache clears ...
    from arcllm.modules.rate_limit import clear_buckets
    clear_buckets()
```

### 5. Config Update (`config.toml`)

Add `burst_capacity` to existing `[modules.rate_limit]` section:

```toml
[modules.rate_limit]
enabled = false
requests_per_minute = 60
burst_capacity = 60
```

---

## ADRs

### ADR-022: Token Bucket Algorithm

**Context**: Need a rate limiting algorithm for throttling outgoing API requests. Options: token bucket, sliding window counter, leaky bucket, adaptive.

**Decision**: Token bucket. Capacity = burst size, refill rate = RPM/60 tokens/sec. Each request consumes one token. Empty bucket = wait for refill.

**Rationale**: Allows bursts (good for agent batches waking up simultaneously) while enforcing average rate. Battle-tested in production systems (nginx, AWS API Gateway, Stripe). Simple to implement (one counter + timestamp). Well-defined math with no edge cases.

**Alternatives rejected**:
- Sliding window — smoother but more complex (needs timestamp tracking), no burst allowance
- Leaky bucket — fixed rate, no bursts, highest latency
- Adaptive — reactive (gets 429s before adjusting), complex, hard to reason about

### ADR-023: Per-Provider Shared State

**Context**: Rate limits are per API key (per provider), not per model instance. Multiple `load_model()` calls for the same provider must share one rate limiter.

**Decision**: Module-level dict in `rate_limit.py` maps provider names to `TokenBucket` instances. Shared across all module instances for the same provider.

**Rationale**: Matches how provider rate limits actually work. If 100 agents share an Anthropic API key with a 60 RPM limit, they must collectively stay under 60 RPM. Per-instance buckets would each think they have 60 RPM, resulting in 6000 RPM hitting the API.

**Threading note**: `asyncio.Lock` protects bucket state within an event loop. For multi-process deployments, each process gets its own rate limiter (acceptable for in-process library; distributed rate limiting is out of scope).

### ADR-024: Sleep Outside Lock

**Context**: When the token bucket is empty and the caller must wait, the lock must be released during the sleep.

**Decision**: Compute wait time inside the lock, release lock, `await asyncio.sleep(wait)`, re-acquire lock to consume token.

**Rationale**: Holding the lock during sleep would serialize all concurrent callers — defeating the purpose of async. Releasing during sleep allows multiple callers to compute their wait times and sleep concurrently. After waking, the caller re-acquires the lock to safely consume a token.

---

## Edge Cases

| Case | Handling |
|------|----------|
| Bucket starts full | First `burst_capacity` requests go through instantly |
| All tokens consumed | Caller waits for `1/refill_rate` seconds per token |
| Multiple concurrent waiters | All compute wait, sleep concurrently, wake and re-acquire lock |
| Bucket accumulates beyond capacity | Capped at `capacity` during refill — no over-filling |
| `burst_capacity` not specified | Defaults to `requests_per_minute` |
| First load_model() sets bucket params | Subsequent same-provider calls share existing bucket |
| Different RPM for same provider | First caller wins (bucket already created) |
| clear_cache() called | All buckets destroyed; next load_model() creates fresh |
| Provider name from inner.name | Reads at construction time; used as bucket registry key |
| Rate limit disabled (kwarg=False) | No RateLimitModule wrapping; adapter called directly |
| RPM = 0 or burst = 0 | ArcLLMConfigError raised at construction |

---

## Test Strategy

One new test file + additions to test_registry.py.

| File | Tests | Priority |
|------|-------|----------|
| `test_rate_limit.py` | TokenBucket + RateLimitModule (all scenarios) | P0 |
| `test_registry.py` (additions) | Rate limit stacking integration | P0 |

### Key Test Scenarios

**TokenBucket:**
- Starts with full capacity
- Acquire consumes a token
- Acquire waits when empty (mock asyncio.sleep)
- Refill adds tokens over time (mock time.monotonic)
- Refill capped at capacity
- Concurrent acquire with asyncio.Lock (multiple tasks)

**RateLimitModule:**
- Invoke delegates to inner after acquiring token
- Logs WARNING when wait > 0
- No log when immediate (wait = 0)
- Passes messages/tools/kwargs through unchanged
- Config validation rejects RPM <= 0 and burst < 1
- burst_capacity defaults to RPM when not specified

**Shared State:**
- Same provider name returns same bucket
- Different provider names get different buckets
- clear_buckets() removes all buckets

**Registry Integration:**
- `load_model("anthropic", rate_limit=True)` wraps with RateLimitModule
- `load_model("anthropic", rate_limit={"requests_per_minute": 120})` uses custom RPM
- `load_model("anthropic", rate_limit=False)` disables even if config enabled
- Stacking order: Retry(Fallback(RateLimit(adapter)))
- clear_cache() clears buckets
