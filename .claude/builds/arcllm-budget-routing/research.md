# arcllm-budget-routing — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-188–D-201 (14 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)

**Phase**: build | **Status**: complete | **Total decisions**: 14

#### Summary

Budget tracking extends TelemetryModule (not a separate module). Per-agent scope with calendar periods (monthly + daily + per-call). Enforcement configurable: warn (enterprise default) or block (federal). Routing replaces the adapter at the innermost stack position, maps data classifications to specific provider+model pairs. Both features follow existing OTel + structured logging patterns for durable audit.




















#### Open Questions

None — all decisions resolved through interactive build session.

#### Key Design Principles

- **Budget IS telemetry** — one module, one cost concern. Can't bypass budget without bypassing telemetry.
- **Router IS the provider** — replaces adapter at innermost position. All observability wraps uniformly.
- **Enforcement is configurable** — enterprise (warn, open) vs federal (block, closed). One toggle governs both.
- **OTel is the durable store** — in-memory accumulators for enforcement, external collectors for audit/queries.
- **Per-agent isolation** — shared-nothing budget tracking. Maps to DID identity.
- **Fail closed in federal mode** — unknown classification, exceeded budget, missing scope all raise errors.

#### Components to Build

**Budget (extends TelemetryModule):**
- `modules/telemetry.py` — extend with BudgetAccumulator, period tracking, enforcement logic (~80 LOC added)
- `types.py` — add `BudgetExceededError` exception
- `config.toml` — add budget fields to `[modules.telemetry]`
- `config.py` — validate new budget config keys

**Routing (new module):**
- `modules/routing.py` — Router class implementing LLMProvider, holds multiple adapters (~150 LOC)
- `config.toml` — add `[modules.routing.rules.*]` sections
- `registry.py` — add routing kwarg to `load_model()`, wire Router as innermost provider
- `config.py` — add routing config validation

#### Architecture Diagram

```
load_model("anthropic", telemetry={...budget...}, budget_scope="agent:007")
    |
    v
Otel (root span, GenAI attributes)
  |
  v
Telemetry + Budget (cost calc, spend tracking, limit enforcement)
  |  - Pre-check: cumulative >= limit? -> block/warn
  |  - Pre-check: estimated cost > per_call_max? -> block
  |  - Post-call: deduct actual cost_usd
  |  - Emit OTel budget attributes + structured logs
  |
  v
Audit (PII-safe metadata logging)
  |
  v
Security (PII redaction, request signing)
  |
  v
Retry (exponential backoff + jitter)
  |
  v
Fallback (provider chain)
  |
  v
RateLimit (token bucket per provider)
  |
  v
Adapter (single provider)
  -- OR --
Router (classification -> provider+model)
  |- AnthropicAdapter (CUI)
  |- OpenAIAdapter (unclassified)
  |- OllamaAdapter (air-gapped)
```

#### Research Insights (via /deepen)

**Enriched**: 2026-02-21 | **Sources**: Codebase analysis (telemetry.py, registry.py, rate_limit.py, config.py, exceptions.py, base.py, adapters/base.py), OTel GenAI semantic conventions, NIST 800-53 controls, scheduler hardening solution, LiteLLM patterns












#### Security Edge Cases Discovered

1. **Negative token count injection**: If a compromised adapter returns `Usage(output_tokens=-1000)`, `_calculate_cost()` returns negative cost, which would increase budget headroom. **Mitigation**: Clamp cost to `max(0.0, cost)` before deducting from accumulator.

2. **Config override via telemetry kwarg**: `load_model(telemetry={"enforcement": "warn", "monthly_limit_usd": 999999})` could override strict config.toml limits. **Mitigation**: `_resolve_module_config()` merges kwarg over config.toml defaults (line 115: `{**config_settings, **kwarg_value}`). This is by design — caller kwargs override config.toml. But if org-wide limits must be enforced, add a `budget_max_override_usd` ceiling in config.toml that can't be exceeded by kwargs.

3. **Scope string in logs**: Budget scope appears in structured logs. From `_logging.py`, `_sanitize()` already escapes `\n`, `\r`, `\t`. Combined with regex validation on the scope string, log injection is mitigated.

4. **Race condition window**: Between pre-check and post-deduct, another concurrent invoke() on the same scope could slip through. In asyncio single-thread, this only matters if there's an `await` between check and deduct — which there is (`await self._inner.invoke()`). **Mitigation**: Pre-check is a conservative estimate. The worst case is two concurrent calls both pass pre-check but together exceed the limit by one call's cost. This is acceptable — budget is an estimate, not a billing system. OTel has the exact record.

5. **Adapter isolation in Router**: Each adapter has its own `httpx.AsyncClient`. No shared state between adapters. A compromised adapter cannot access another adapter's client, keys, or state. This is inherent in the current `BaseAdapter` design.

#### New Risks Discovered

1. **Clock manipulation on budget periods** — If system clock jumps backward (NTP correction, VM snapshot restore), monthly/daily accumulators could reset prematurely. Mitigation: Use monotonic clock for within-period tracking, wall clock only for period boundary detection. Log clock jumps as audit events.

2. **Budget exhaustion as DoS** — A malicious caller could deliberately burn budget to deny service to a legitimate agent sharing the scope. Mitigation: D-189's per-agent scope isolation prevents this. Each agent has its own accumulator.

3. **Router config hot-reload** — If config.toml is modified while Router is running, stale routing rules persist until restart. This is consistent with all other modules (none support hot-reload). Document as known limitation.

4. **Multi-provider cost inconsistency** — Router routes to different providers with different pricing. TelemetryModule's cost rates are set at init from a single provider's metadata. With Router, the cost rate must match the actually-selected provider. **Critical**: TelemetryModule must get pricing from the response's actual provider, not the init-time config. This requires Router to inject pricing metadata or TelemetryModule to look up pricing dynamically.

#### Implementation Priority

Based on codebase analysis, the implementation order should be:

1. **BudgetAccumulator + scope validation** (standalone, no module changes)
2. **Extend TelemetryModule with budget** (pre/post hooks around existing invoke)
3. **Extend exceptions.py** with `ArcLLMBudgetError`
4. **Update config.toml** (budget fields under `[modules.telemetry]`)
5. **RoutingModule** (new module implementing LLMProvider)
6. **Update registry.py** (add routing/budget_scope kwargs to `load_model()`)
7. **Tests** (unit → integration → security)

Estimated total new LOC: ~230 (budget ~80, routing ~150)

---

---

## Per-decision deepen notes

### D-188 — Research Insights (via /deepen)


TelemetryModule is 106 LOC (`modules/telemetry.py`). Adding budget tracking requires extending several precise contracts:

1. **`_VALID_CONFIG_KEYS` (line 14)**: Must add budget keys — `monthly_limit_usd`, `daily_limit_usd`, `per_call_max_usd`, `alert_threshold_pct`, `enforcement`, `budget_scope`. Without this, `validate_config_keys()` rejects them at construction.

2. **Constructor validation (line 35-54)**: Budget limits must be validated `>= 0` using same pattern as cost fields. `enforcement` must be validated against `{"warn", "block"}`. `budget_scope` format must be validated (see D-189 insights).

3. **`invoke()` flow (line 69-105)**: Budget checks inject at two points:
   - **Pre-call** (before `self._inner.invoke()`): Check cumulative + estimate against limits. This is new — current invoke() has no pre-call logic.
   - **Post-call** (after response): Deduct actual `cost_usd` from accumulator. This hooks after `_calculate_cost()`.

4. **OTel span attributes (line 84-85)**: Currently sets `arcllm.telemetry.duration_ms` and `arcllm.telemetry.cost_usd`. Budget adds: `arcllm.budget.scope`, `arcllm.budget.cumulative_usd`, `arcllm.budget.daily_usd`, `arcllm.budget.monthly_limit_usd`, `arcllm.budget.daily_limit_usd`, `arcllm.budget.enforcement`, `arcllm.budget.action` (allowed/warned/blocked).

5. **OTel GenAI semantic conventions**: The official `gen_ai.*` namespace (set by OtelModule at lines 212-220) does NOT include cost or budget attributes. Our `arcllm.budget.*` namespace is correct — custom vendor attributes under our own prefix.

**Key pattern**: TelemetryModule returns `response.model_copy(update={"cost_usd": cost})` (line 87). Budget warning metadata should use the same pattern: `response.model_copy(update={"cost_usd": cost, "metadata": {"budget_warning": True}})` when in warn mode.

### D-189 — Research Insights (via /deepen)


From the scheduler hardening solution (Fix 1: Unicode NFKC normalization), scope strings need the same defense:

```python
import re
import unicodedata

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")

def _validate_budget_scope(scope: str) -> None:
    normalized = unicodedata.normalize("NFKC", scope)
    if normalized != scope:
        raise ArcLLMConfigError(
            f"budget_scope contains non-ASCII characters: {scope!r}"
        )
    if not _SCOPE_RE.match(scope):
        raise ArcLLMConfigError(
            f"Invalid budget_scope '{scope}'. Must be lowercase alphanumeric "
            "with colons, dots, hyphens. Max 128 chars. Example: 'agent:agent-007'"
        )
```

**Why**: Prevents scope string injection (`agent:007; DROP TABLE`), path traversal (`../../../etc`), and Unicode homoglyph attacks. Mirrors `_validate_provider_name()` in `config.py:126-144` which uses `_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")`.

**Accumulator key**: Use validated scope string directly as dict key. No need for hashing — the regex ensures it's safe for use as a dict key, OTel attribute value, and log field.

### D-190 — Research Insights (via /deepen)


Router replaces the adapter at the innermost position. In `registry.py`, the current flow is:

```python
# Line 205: adapter created
result: LLMProvider = adapter_class(config, model_name, resolved_api_key=resolved_api_key)
# Lines 208-253: modules wrap in order
```

Router changes this to:

```python
# If routing enabled, Router replaces the single adapter
routing_config = _resolve_module_config("routing", routing)
if routing_config is not None:
    from arcllm.modules.routing import RoutingModule
    result = RoutingModule(routing_config, ...)  # holds multiple adapters internally
else:
    result = adapter_class(config, model_name, resolved_api_key=resolved_api_key)
# Then normal module wrapping continues
```

**Critical**: Router must implement `LLMProvider` protocol (has `name`, `model_name`, `invoke()`, `validate_config()`, `close()`). It selects which internal adapter to delegate to based on `classification` kwarg in `invoke()`.

**Router.close()**: Must close ALL internal adapters:
```python
async def close(self) -> None:
    for adapter in self._adapters.values():
        await adapter.close()
```

**Router.name/model_name**: Return the default route's provider name (for logging/span context when no classification is provided).

### D-191 — Research Insights (via /deepen)


Each route in config maps to a separate adapter instance. At Router init:

```python
def __init__(self, routing_config: dict[str, Any]) -> None:
    self._adapters: dict[str, LLMProvider] = {}
    for classification, rule in routing_config["rules"].items():
        provider_name = rule["provider"]
        model_name = rule["model"]
        # Load adapter using existing registry machinery
        adapter_class = _get_adapter_class(provider_name)
        provider_config = load_provider_config(provider_name)
        adapter = adapter_class(provider_config, model_name)
        self._adapters[classification] = adapter
```

**Gotcha from codebase**: `_get_adapter_class()` and provider config are cached at module level (`_adapter_class_cache`, `_provider_config_cache`). Router can reuse these caches. But each adapter instance needs its own `httpx.AsyncClient` — do NOT share clients across adapters.

**Vault key resolution**: Each adapter may need a different vault path. Router must resolve API keys per-provider, same as `load_model()` does at lines 187-199.

### D-193 — Research Insights (via /deepen)


The `_bucket_registry` pattern in `rate_limit.py:63` is the exact model:

```python
# Module-level shared state (same pattern as rate_limit.py)
_budget_registry: dict[str, "BudgetAccumulator"] = {}

def _get_or_create_accumulator(scope: str) -> "BudgetAccumulator":
    if scope not in _budget_registry:
        _budget_registry[scope] = BudgetAccumulator()
    return _budget_registry[scope]

def clear_budgets() -> None:
    """For test isolation — must be called in registry.clear_cache()."""
    _budget_registry.clear()
```

**Critical**: Must add `clear_budgets()` call to `registry.py:clear_cache()` (line 21-35) alongside existing `clear_buckets()` and `reset_sdk()`. Without this, tests will share budget state.

**Floating-point precision**: Current `_calculate_cost()` uses Python `float` (IEEE 754 double). For budget tracking, accumulated costs over thousands of calls could drift. However:
- At $0.001 per call, 10,000 calls = $10. Float64 has ~15 significant digits. Drift at this scale is ~1e-12 USD — irrelevant.
- Using `Decimal` would break compatibility with existing `cost_usd: float` on LLMResponse and all OTel attributes (which are float64).
- **Decision**: Keep `float`. The accumulator is for enforcement, not billing. OTel spans have the per-call exact amounts for billing reconciliation.

**Race condition analysis**: Python's GIL protects dict operations in CPython. For asyncio (single-threaded), there's no true concurrency risk on `_budget_registry[scope] += cost`. However, if the pre-check and post-deduct are separated by an `await`:
```python
# SAFE pattern: check + deduct in same synchronous block after await
response = await self._inner.invoke(...)  # yields to event loop
cost = self._calculate_cost(response.usage)
accumulator.deduct(cost)  # synchronous — no yield between check and write
```
The scheduler hardening solution (Fix 3: stale circuit breaker) warns about reading state before an operation and assuming it's still valid after. Our accumulator must read-check-deduct atomically (no `await` between check and deduct).

### D-194 — Research Insights (via /deepen)


**Period boundary edge case**: A call starts at 23:59:59.999 on Jan 31, completes at 00:00:00.500 on Feb 1.

- **Which period is charged?** The period when the cost is *deducted* (post-call). Since deduction happens after `await self._inner.invoke()`, the call is charged to February.
- **Is this correct?** Yes — it's the standard "cash basis" accounting model. The cost isn't incurred until the response arrives. This aligns with Anti-Deficiency Act obligation timing.

**Daily reset logic**:
```python
from datetime import date, datetime, timezone

class BudgetAccumulator:
    def __init__(self) -> None:
        self._monthly_spend: float = 0.0
        self._daily_spend: float = 0.0
        self._current_month: int = 0  # YYYYMM
        self._current_day: int = 0    # YYYYMMDD

    def _maybe_reset(self) -> None:
        now = datetime.now(timezone.utc)
        month_key = now.year * 100 + now.month
        day_key = month_key * 100 + now.day
        if month_key != self._current_month:
            self._monthly_spend = 0.0
            self._daily_spend = 0.0
            self._current_month = month_key
            self._current_day = day_key
        elif day_key != self._current_day:
            self._daily_spend = 0.0
            self._current_day = day_key
```

**Why integer keys (YYYYMM, YYYYMMDD) instead of date objects**: Cheaper comparison, no timezone localization complexity, no DST edge cases (we use UTC).

**NIST SA-2 compliance**: The accumulator acts as a "sub-allotment tracker." NIST SA-2 requires "the organization determines, documents, and allocates resources required to adequately protect the information system." Our per-agent budget scope maps directly to per-agent resource allocation.

### D-195 — Research Insights (via /deepen)


`BaseAdapter._resolve_defaults()` (adapters/base.py:74-81) resolves max_tokens from kwargs > model meta > default (4096). The pre-flight estimate needs the same resolution:

```python
# In TelemetryModule.invoke(), before inner call:
max_tokens = kwargs.get("max_tokens")
if max_tokens is None and hasattr(self._inner, "_model_meta"):
    max_tokens = self._inner._model_meta.max_output_tokens if self._inner._model_meta else 4096
else:
    max_tokens = max_tokens or 4096

estimated_cost = max_tokens * self._cost_output / 1_000_000
```

**But TelemetryModule can't access inner adapter's _model_meta** — it's wrapped behind potentially multiple modules. Simpler approach: accept `max_tokens` from kwargs, fall back to `defaults.max_tokens` from global config (4096). The estimate is intentionally conservative (upper bound).

### D-197 — Research Insights (via /deepen)


Classification passes through the entire module stack via `**kwargs`:

```python
await model.invoke(messages, tools, classification="cui")
```

Every module's `invoke()` already passes `**kwargs` to `self._inner.invoke()`:
- `TelemetryModule.invoke()` line 77: `await self._inner.invoke(messages, tools, **kwargs)`
- `AuditModule.invoke()` line 48: `await self._inner.invoke(messages, tools, **kwargs)`
- `BaseModule.invoke()` line 87: `await self._inner.invoke(messages, tools, **kwargs)`

So `classification` kwarg naturally flows down to the Router without any intermediate module changes.

**Router selection logic**:
```python
async def invoke(self, messages, tools=None, **kwargs):
    classification = kwargs.pop("classification", self._default_classification)
    # ... validate classification, select adapter ...
    return await selected_adapter.invoke(messages, tools, **kwargs)
```

**Important**: `kwargs.pop()` not `kwargs.get()` — remove classification before passing to the actual adapter, since adapters don't know about classification.

### D-199 — Research Insights (via /deepen)


**BudgetExceededError** must fit the existing exception hierarchy (`exceptions.py`):

```python
class ArcLLMBudgetError(ArcLLMError):
    """Raised when a budget limit would be exceeded."""

    def __init__(
        self,
        scope: str,
        limit_type: str,  # "monthly", "daily", "per_call"
        limit_usd: float,
        current_usd: float,
        estimated_usd: float | None = None,
    ) -> None:
        self.scope = scope
        self.limit_type = limit_type
        self.limit_usd = limit_usd
        self.current_usd = current_usd
        self.estimated_usd = estimated_usd
        super().__init__(
            f"Budget exceeded for {scope}: {limit_type} limit ${limit_usd:.2f}, "
            f"current ${current_usd:.2f}"
            + (f", estimated ${estimated_usd:.2f}" if estimated_usd else "")
        )
```

**Why `ArcLLMBudgetError` not `BudgetExceededError`**: Follows existing naming convention (`ArcLLM` prefix on all exceptions). Extends `ArcLLMError` (not `ArcLLMConfigError`) because this is a runtime error, not a configuration error.

**Warn mode metadata**: `response.metadata` is `dict[str, Any] | None` on LLMResponse. When warning:
```python
metadata = response.metadata or {}
metadata["budget_warning"] = True
metadata["budget_scope"] = self._scope
metadata["budget_cumulative_usd"] = accumulator.monthly_spend
response = response.model_copy(update={"metadata": metadata})
```

**Alert threshold**: At 80% (default), emit a structured log + OTel event but don't block/warn. This is an early notification before the hard limit:
```python
if accumulator.monthly_spend / monthly_limit >= alert_threshold:
    tel_span.add_event("budget_alert", {
        "arcllm.budget.scope": self._scope,
        "arcllm.budget.monthly_spend_usd": accumulator.monthly_spend,
        "arcllm.budget.monthly_limit_usd": monthly_limit,
        "arcllm.budget.threshold_pct": alert_threshold * 100,
    })
```

### D-200 — Research Insights (via /deepen)


From user's input: "enterprise mode (warn, route to default) vs federal mode (block, fail closed)."

Router config needs `default_classification` and respects the same `enforcement` toggle:

```toml
[modules.routing]
enabled = true
enforcement = "warn"           # "warn" = enterprise, "block" = federal
default_classification = "unclassified"

[modules.routing.rules.cui]
provider = "anthropic"
model = "claude-sonnet-4-6"

[modules.routing.rules.unclassified]
provider = "openai"
model = "gpt-4o-mini"
```

When `classification` kwarg doesn't match any rule:
- `enforcement = "warn"`: Log warning, route to `default_classification`
- `enforcement = "block"`: Raise `ArcLLMConfigError("Unknown classification '{classification}' and enforcement is 'block'")`

### D-201 — Research Insights (via /deepen)


Following existing test patterns (test_telemetry.py is 385 lines, test_rate_limit.py):

```
tests/
  test_budget.py               # Budget accumulator, limits, periods, enforcement
    TestBudgetAccumulator      # Reset logic, period boundaries, float precision
    TestBudgetEnforcement      # Block mode, warn mode, alert threshold
    TestBudgetPreFlight        # Per-call max estimation
    TestBudgetValidation       # Config validation, scope validation
    TestBudgetOtelAttributes   # Span attributes for budget events

  test_routing.py              # Classification routing
    TestRoutingSelection       # Classification -> adapter mapping
    TestRoutingUnknown         # Unknown classification behavior
    TestRoutingAdapterLifecycle # Eager loading, close(), health
    TestRoutingValidation      # Config validation, rules structure
    TestRoutingKwargsFlow      # classification kwarg pop + passthrough

  test_budget_telemetry.py     # Integration: budget inside telemetry stack
    TestBudgetTelemetryIntegration  # Full module stack with budget + audit + otel

  test_routing_stack.py        # Integration: router with full module stack
    TestRoutingStackIntegration # Router as innermost with full module wrapping

  security/
    test_budget_security.py    # Budget bypass, scope injection, negative costs
    test_routing_security.py   # Classification downgrade, adapter isolation
```

**Test helper pattern** from test_telemetry.py: `_make_inner()` creates a MagicMock with `spec=LLMProvider`. Budget tests need an extended version that also returns configurable `cost_usd` values.

---
