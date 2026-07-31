# SDD: Telemetry Module

> System design for ArcLLM Step 10.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 10 adds the telemetry module to the system: a structured logger that measures wall-clock timing, reads token usage from LLMResponse, calculates cost from provider pricing metadata, and emits a single log line per invoke() call. It validates cross-layer data flow — pricing data originates in provider TOML model metadata, gets injected into telemetry config by load_model(), and consumed by TelemetryModule.

Key design insight: telemetry wraps outermost because operators care about **total wall-clock** including retries, fallback attempts, and rate-limit waits. The Usage object in LLMResponse comes from the actual successful provider call, but the timing captures everything around it.

Design priorities:
1. **Operational visibility** — structured log with all fields needed for dashboards and alerts
2. **Automatic pricing** — load_model() bridges provider TOML pricing to telemetry config
3. **Conditional fields** — cache tokens only logged when present (reduces noise)
4. **Configurable** — log level, cost overrides, all via standard config dict pattern

---

## Directory Map

```
src/arcllm/
├── modules/
│   ├── __init__.py                    # MODIFY: Add TelemetryModule export
│   ├── base.py                        # UNCHANGED
│   ├── retry.py                       # UNCHANGED
│   ├── fallback.py                    # UNCHANGED
│   ├── rate_limit.py                  # UNCHANGED
│   └── telemetry.py                   # NEW: TelemetryModule
├── registry.py                        # MODIFY: Add telemetry= kwarg, pricing injection
├── __init__.py                        # MODIFY: Add TelemetryModule to lazy imports
├── config.toml                        # MODIFY: Add log_level to [modules.telemetry]
tests/
├── test_telemetry.py                  # NEW: Full test suite (21 tests)
├── test_registry.py                   # MODIFY: Add telemetry stacking tests (7 tests)
```

---

## Component Design

### 1. TelemetryModule (`modules/telemetry.py`)

A BaseModule subclass that wraps invoke() to measure timing, read token usage, calculate cost, and emit a structured log line.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `_cost_input` | `float` | Cost per 1M input tokens (default: 0.0) |
| `_cost_output` | `float` | Cost per 1M output tokens (default: 0.0) |
| `_cost_cache_read` | `float` | Cost per 1M cache read tokens (default: 0.0) |
| `_cost_cache_write` | `float` | Cost per 1M cache write tokens (default: 0.0) |
| `_log_level` | `int` | Python logging level (default: logging.INFO) |

| Method | Purpose |
|--------|---------|
| `__init__(config, inner)` | Validate config, extract pricing and log level |
| `_calculate_cost(usage) -> float` | Compute USD cost from token counts and pricing |
| `async invoke(messages, tools, **kwargs) -> LLMResponse` | Time the call, compute cost, log metrics, return response |

#### Configuration

From `config.toml [modules.telemetry]`:
```toml
[modules.telemetry]
enabled = false
log_level = "INFO"
```

Additional config keys injected by `load_model()` from provider model metadata:
- `cost_input_per_1m` (float, default 0.0)
- `cost_output_per_1m` (float, default 0.0)
- `cost_cache_read_per_1m` (float, default 0.0)
- `cost_cache_write_per_1m` (float, default 0.0)

#### Logic Flow

```
__init__(config, inner):
    super().__init__(config, inner)
    _cost_input = config.get("cost_input_per_1m", 0.0)
    _cost_output = config.get("cost_output_per_1m", 0.0)
    _cost_cache_read = config.get("cost_cache_read_per_1m", 0.0)
    _cost_cache_write = config.get("cost_cache_write_per_1m", 0.0)
    validate: all costs >= 0 (raise ArcLLMConfigError if negative)
    log_level_name = config.get("log_level", "INFO")
    validate: log_level_name in {DEBUG, INFO, WARNING, ERROR, CRITICAL}
    _log_level = getattr(logging, log_level_name)

_calculate_cost(usage):
    cost = (input_tokens * _cost_input + output_tokens * _cost_output) / 1_000_000
    if cache_read_tokens:  cost += cache_read_tokens * _cost_cache_read / 1_000_000
    if cache_write_tokens: cost += cache_write_tokens * _cost_cache_write / 1_000_000
    return cost

invoke(messages, tools, **kwargs):
    start = time.monotonic()
    response = await inner.invoke(messages, tools, **kwargs)
    elapsed = time.monotonic() - start
    cost = _calculate_cost(response.usage)
    duration_ms = round(elapsed * 1000, 1)
    parts = [provider, model, duration_ms, input/output/total tokens]
    if cache_read_tokens is not None: parts += cache_read_tokens
    if cache_write_tokens is not None: parts += cache_write_tokens
    parts += [cost_usd, stop_reason]
    logger.log(_log_level, "LLM call | %s", " ".join(parts))
    return response
```

### 2. Pricing Injection (`registry.py` changes)

#### load_model() signature change

```python
def load_model(
    provider: str,
    model: str | None = None,
    *,
    retry: bool | dict | None = None,
    fallback: bool | dict | None = None,
    rate_limit: bool | dict | None = None,
    telemetry: bool | dict | None = None,  # NEW
) -> LLMProvider:
```

#### Stacking order change

Current (Step 8): `Retry(Fallback(RateLimit(adapter)))`
New (Step 10): `Telemetry(Retry(Fallback(RateLimit(adapter))))`

Telemetry wraps everything (outermost), rate limit wraps adapter (innermost).

#### Pricing injection logic

```python
telemetry_config = _resolve_module_config("telemetry", telemetry)
if telemetry_config is not None:
    # Inject pricing from provider model metadata
    model_meta = config.models.get(model_name)
    if model_meta is not None:
        telemetry_config.setdefault("cost_input_per_1m", model_meta.cost_input_per_1m)
        telemetry_config.setdefault("cost_output_per_1m", model_meta.cost_output_per_1m)
        telemetry_config.setdefault("cost_cache_read_per_1m", model_meta.cost_cache_read_per_1m)
        telemetry_config.setdefault("cost_cache_write_per_1m", model_meta.cost_cache_write_per_1m)
    result = TelemetryModule(telemetry_config, result)
```

Key design: `setdefault()` means explicit overrides in the kwarg dict take precedence over model metadata. This allows:
- Default: pricing auto-injected from anthropic.toml model metadata
- Override: `telemetry={"cost_input_per_1m": 5.0}` for custom pricing

### 3. Config Update (`config.toml`)

Add `log_level` to existing `[modules.telemetry]` section:

```toml
[modules.telemetry]
enabled = false
log_level = "INFO"
```

---

## ADRs

### ADR-025: Structured Logging Only (No Callback/Accumulator)

**Context**: Need a telemetry output mechanism. Options: structured logging, callback function, in-memory accumulator, or combination.

**Decision**: Structured logging only. Single log line per invoke() with key=value pairs.

**Rationale**: Simplest approach that provides immediate value. Log aggregation systems (ELK, Datadog, CloudWatch) can parse key=value structured logs for dashboards and alerts. No functions in config dict (config must be serializable TOML). Budget tracking (Step 12) and OTel spans (Step 13) handle their own output — telemetry doesn't need to feed them directly.

**Alternatives rejected**:
- Callback function — requires passing callable in config, breaks TOML serializability
- In-memory accumulator — adds state management, unclear who consumes aggregated data
- Both logging + callback — over-engineering for current needs

### ADR-026: Cost Calculation from Provider Model Metadata

**Context**: Need per-call cost calculation. Pricing data exists in provider TOML model metadata (cost_input_per_1m, etc.).

**Decision**: Calculate cost in TelemetryModule using pricing injected by load_model() from ProviderConfig. Formula: `(tokens * cost_per_1m) / 1_000_000`.

**Rationale**: Pricing already maintained in provider TOML for each model. load_model() bridges the gap using setdefault() injection — TelemetryModule receives pricing as config keys, never imports ProviderConfig. Clean separation: providers own pricing data, registry handles injection, telemetry does math.

### ADR-027: Telemetry Outermost in Module Stack

**Context**: Where should telemetry sit in the module wrapping order?

**Decision**: Outermost: `Telemetry(Retry(Fallback(RateLimit(adapter))))`.

**Rationale**: Operators need total wall-clock time including retries, fallback switches, and rate-limit waits. If telemetry were innermost, it would only measure the final successful adapter call — missing the retry overhead that's often the most interesting signal. Token/cost data comes from the response regardless of position.

**Alternatives rejected**:
- Innermost (just adapter time) — misses retry/fallback/rate-limit overhead
- Between retry and fallback — arbitrary, misses retry overhead

### ADR-028: Conditional Cache Token Fields

**Context**: Not all providers support prompt caching. When cache tokens are None, should we log `cache_read_tokens=0` or omit the field entirely?

**Decision**: Omit cache token fields when their value is None in the Usage object.

**Rationale**: Reduces log noise for providers that don't use caching (OpenAI currently, Ollama). Fields only appear when the response actually includes cache data. Log parsing rules can check for field presence to detect caching behavior.

---

## Edge Cases

| Case | Handling |
|------|----------|
| No pricing configured (all zeros) | cost_usd=0.000000 — valid, just means cost tracking not configured |
| Missing cost fields in config | Default to 0.0 each (free tier or unpriced model) |
| Negative cost value | ArcLLMConfigError raised at construction |
| Unknown model (no metadata) | Pricing injection skipped, defaults to 0.0 |
| Cache tokens = 0 (not None) | Logged as cache_read_tokens=0 (present but zero) |
| Cache tokens = None | Field omitted from log line entirely |
| Zero input/output tokens | cost_usd=0.000000 — valid for empty responses |
| Very large token counts (1M+) | Floating-point precision sufficient for USD amounts |
| Inner provider raises exception | Exception propagates — no log line emitted (call failed) |
| Invalid log level string | ArcLLMConfigError raised at construction |
| telemetry=False kwarg | Disables even if config.toml has enabled=true |
| Explicit cost override in kwarg dict | setdefault() preserves explicit values over metadata |

---

## Test Strategy

One new test file + additions to test_registry.py.

| File | Tests | Priority |
|------|-------|----------|
| `test_telemetry.py` | TelemetryModule (all scenarios, 21 tests) | P0 |
| `test_registry.py` (additions) | Telemetry stacking + pricing injection (7 tests) | P0 |

### Key Test Scenarios

**TelemetryModule Core:**
- Invoke delegates to inner provider
- Passes tools and kwargs through unchanged
- Returns response unchanged (same object reference)
- Logs timing and usage fields (mocked time.monotonic)
- Logs cost calculation (verified math)
- Provider name and model name from inner

**Cache Token Handling:**
- Logs cache_read_tokens and cache_write_tokens when present
- Omits cache fields when absent (None)
- Cost includes cache token costs when present
- Cost correctly accounts for all 4 token types

**Validation:**
- Negative cost_input_per_1m rejected
- Negative cost_output_per_1m rejected
- Missing cost fields default to 0.0
- Invalid log_level rejected

**Cost Calculation (unit):**
- Basic cost (input + output only)
- Cost with cache_read tokens
- Cost with all 4 token types
- Zero cost when no pricing configured
- Exact cost at 1M token boundary

**Log Level:**
- Custom log_level=DEBUG: not visible at INFO, visible at DEBUG

**Registry Integration:**
- load_model with telemetry=True wraps with TelemetryModule
- Pricing injected from anthropic.toml model metadata
- Haiku pricing differs from Sonnet
- Explicit cost overrides metadata via setdefault
- Full stack: Telemetry(Retry(Fallback(RateLimit(adapter))))
- telemetry=False overrides config.toml enabled=true
