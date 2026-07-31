# PLAN — Load Balancing (Intra-Provider Endpoint/Key Distribution)

**Status**: COMPLETE
**Spec**: 017-load-balancing
**Estimated tasks**: 11
**Estimated new tests**: ~33 (24 unit + 3 concurrency + 6 integration)
**Boundary**: arcllm only. Cross-agent scheduling stays in arcrun (D-455) — no arcrun edits in this plan.

TDD throughout: write the failing test first (RED), implement the minimum to pass
(GREEN), then refactor. Every task leaves `mypy --strict` and `ruff check` clean.

---

## Phase 1: Config Foundation (Tasks 1-2)

### T17.1 — Add EndpointConfig + pool + module config to config.py
- [x] Add `EndpointConfig(BaseModel)`: `base_url`, `api_key_env=""`, `vault_path=""`, `weight=1`
- [x] Reuse the HTTPS-for-remote validator on `EndpointConfig.base_url` (share with `ProviderSettings`)
- [x] Validate `weight >= 0`; require at least one of `api_key_env`/`vault_path` when `api_key_required`
- [x] Add `endpoints: list[EndpointConfig] = []` to `ProviderConfig` and parse `[[endpoints]]` in `load_provider_config`
- [x] ~~Add `LoadBalanceModuleConfig(ModuleConfig)`~~ — DEVIATION: no other module (rate_limit, circuit_breaker, retry, fallback, telemetry, queue, audit, security, otel) has a typed per-module config subclass; all parse `config: dict[str, Any]` inline with `.get()` defaults + `validate_config_keys()`. Adding one typed class solely for load_balance would be inconsistent scope creep (YAGNI/DRY). `LoadBalancerModule` follows the `CircuitBreakerModule` pattern instead.

**Acceptance**:
- [x] Provider TOML with `[[endpoints]]` parses into a typed `list[EndpointConfig]`
- [x] TOML without `[[endpoints]]` yields `endpoints == []` (no breaking change)
- [x] Remote HTTP endpoint rejected; localhost allowed
- [x] Existing config tests pass

### T17.2 — Add config.toml + provider TOML templates
- [x] Add `[modules.load_balance]` section to config.toml (`enabled=false`, `strategy="weighted_round_robin"`, sticky/health defaults)
- [x] Add commented `[[endpoints]]` template to `providers/openai.toml`
- [x] Add an `[[endpoints]]` example to a self-hosted provider TOML (e.g. `vllm.toml`) if present

**Acceptance**:
- [x] Config loads without errors; `load_balance` module config resolves
- [x] Existing config-loading tests pass

---

## Phase 2: Endpoint Health + Registry (Tasks 3-4)

### T17.3 — Write test_load_balancer.py health + registry cases (TDD RED)
- [x] Test `_EndpointHealth` CLOSED → OPEN after `failure_threshold`
- [x] Test OPEN → HALF_OPEN after `cooldown_seconds`, → CLOSED on probe success, → OPEN on probe failure
- [x] Test `_get_or_create_pool` returns the same `_PoolState` for the same pool_id
- [x] Test `clear_pools()` resets registry
- [x] Test pool_id is stable for identical endpoint sets, distinct for different sets

**Acceptance**:
- [x] Health + registry tests written and failing for the right reason (module absent)

### T17.4 — Implement _EndpointHealth + shared registry (TDD GREEN)
- [x] `_EndpointHealth` per-endpoint circuit reusing circuit_breaker.py semantics (failure_threshold, cooldown, half_open_max)
- [x] Module-level `_pool_registry: dict[str, _PoolState]`; `_PoolState{cursor, health, lock: asyncio.Lock}`
- [x] `_get_or_create_pool(pool_id, endpoints)` and `clear_pools()`
- [x] Stable `pool_id` hash over provider + endpoint identities (base_url + key-source)
- [x] Wire `clear_pools()` into `registry.clear_cache()`

**Acceptance**:
- [x] All T17.3 tests pass
- [x] Lock guards only cursor/health mutation (no `await` under lock)
- [x] Coverage on new health/registry code >= 90%

### Research Insights — added tests/behavior (health + registry)
- [x] **Escalating cooldown (Envoy pattern):** repeat-offender endpoint stays OPEN longer (cooldown grows with consecutive-ejection count); test a flapping endpoint is quarantined longer than a one-off failure.
- [x] **Recovery jitter:** effective cooldown carries ±10–20% jitter so pooled agents don't all transition to HALF_OPEN on the same tick (herd guard beyond `half_open_max_calls`).
- [x] **`Retry-After` on 429:** a 429 sets cooldown to `max(cooldown_seconds, Retry-After)`; test the rate-limited endpoint isn't re-probed early.
- [x] **Re-read under lock:** health-aware selection reads `_PoolState.health[ep_id]` inside the lock at decision time (no stale snapshot) — guards concurrent lost updates (solutions/2026-02-16).
- [x] **Bounded state:** assert `_PoolState.health` only ever keys by fixed endpoint ids (no per-session/per-request keys → no unbounded growth).

---

## Phase 3: Strategies (Tasks 5-6)

### T17.5 — Write strategy tests (TDD RED)
- [x] Weighted RR distribution over 400 calls matches weights {2,1,1} within tolerance
- [x] Cursor advances monotonically and wraps
- [x] `weight=0` endpoint never selected; all-zero → `ArcLLMConfigError`
- [x] Health-aware skips a tripped endpoint, resumes after cooldown
- [x] All endpoints unhealthy → `PoolExhaustedError`
- [x] Sticky: same key → same endpoint; missing key → RR; unhealthy pin → evict to healthy

**Acceptance**:
- [x] Strategy tests written and failing

### T17.6 — Implement strategies + PoolExhaustedError (TDD GREEN)
- [x] `select_weighted_rr` (default) — shared cursor under lock, weighted sequence
- [x] `select_health_aware` — skip OPEN endpoints, probe on cooldown, raise `PoolExhaustedError` when none healthy
- [x] `select_sticky` — stateless hash pin over weighted sequence, evict on unhealthy
- [x] `PoolExhaustedError` (arcllm error subclass)
- [x] `weight=0` exclusion; all-zero construction guard

**Acceptance**:
- [x] All T17.5 tests pass
- [x] Coverage on strategy code >= 90%

### Research Insights — added tests (strategies)
- [x] **weight=0 zero-selections:** across many calls a weight=0 endpoint receives *exactly zero* selections (assert count, not just "excluded from sequence") — drain-for-maintenance semantics.
- [x] **Sticky skew (documented limitation):** a hot `sticky_key` pins to one endpoint with no load relief until unhealthy; test documents current stateless-hash behavior. If owner adopts bounded-load overflow (CHWBL, arXiv 1608.01350), add an overflow-on-load test — deferred by default.
- [x] **Sticky isolation:** distinct `sticky_key`s spread across the weighted sequence; a caller's key only affects its own traffic (no cross-tenant steering, ASI03).
- [x] **All-unhealthy correlated vs isolated:** all-OPEN surfaces `PoolExhaustedError`; under a wrapping provider CircuitBreaker, a provider-wide failure trips the provider CB (not N endpoint circuits redundantly) — documented in module docstring; provider-level CB behavior is exercised in the existing circuit_breaker.py test suite (out of this module's scope to duplicate).

---

## Phase 4: Concurrency Proof (Task 7)

### T17.7 — Write + pass interleaving concurrency test
- [x] `test_load_balancer_concurrency.py`: M concurrent `select_endpoint()` gated by `asyncio.Barrier`/`Event` so tasks truly interleave
- [x] Assert issued-slot multiset == expected round-robin multiset (no double-issue, no skip)
- [x] Concurrent invokes against a failing endpoint: health recorded exactly once per failure (no lost updates)
- [x] `clear_pools()` isolates each test

**Acceptance**:
- [x] Interleaving test proves the lock is genuinely exercised (instrumented-lock acquire-count + mutual-exclusion assertions) and slot issuance is correct under forced contention. DEVIATION: a literal "remove the lock and watch the real test fail" is not meaningful here — `_select_weighted_rr`/`_select_health_aware`/`_select_sticky` contain zero `await` inside their locked sections (by design, ADR-6), so asyncio's cooperative single-threaded scheduler cannot preempt mid-section regardless of whether a lock is present; there is no yield point to race on. A companion test (`test_barrier_methodology_catches_a_real_unlocked_race`) reproduces the anti-pattern (unlocked read-modify-write with an `await` in between) under the identical Barrier harness and shows it corrupts state — proving the harness itself would catch a real regression, e.g. a future refactor that added an `await` inside an unlocked critical section.
- [x] No sequential-mock false pass (barrier forces real interleaving)

### Research Insights — added tests (concurrency proof)
- [x] **Contention micro-benchmark:** N concurrent `invoke()` calls complete in ~max(invoke latency) not sum (proves await-outside-lock, no singleton bottleneck); lock hold time asserted to be a tiny fraction of invoke latency via an instrumented timed-lock subclass (a literal "sub-microsecond" absolute bound was judged too flaky for CI — used a relative bound instead: hold time < latency/10).
- [x] **Recovery thundering-herd:** M concurrent callers arriving at cooldown-expiry yield exactly `half_open_max_calls` HALF_OPEN probes to the recovering endpoint; the rest route to the healthy endpoint — no stampede. GOTCHA found during implementation: freezing `time.monotonic` globally (as done in the synchronous health-state-machine tests) hangs real `asyncio.sleep`/event-loop timing, since `asyncio`'s default loop uses `time.monotonic()` for its own clock; this test instead lets a tiny real `cooldown_seconds` elapse via a tiny real `asyncio.sleep`, and only mocks `random.uniform` for determinism.

---

## Phase 5: Module + Registry Integration (Tasks 8-10)

### T17.8 — Write LoadBalancerModule + integration tests (TDD RED)
- [x] `LoadBalancerModule` builds one adapter per endpoint; selects per invoke; delegates to chosen adapter. DEVIATION: subclasses `LLMProvider` directly (matching `RoutingModule`'s pool-of-adapters shape), not `BaseModule` (which assumes a single `inner`) — see README Learnings.
- [x] Test single-endpoint / empty-pool pass-through (FR-15)
- [x] Test per-endpoint key from env/vault, never a literal field (FR-18)
- [x] Test OTel span `arcllm.load_balance` attributes (endpoint, strategy, healthy-count)
- [x] Test `load_model("openai", load_balance=True|dict|False|None)` matrix
- [x] Test stack: RateLimit/Retry/Queue wrap LB; LB innermost
- [x] Test `MODULE_NAMES` contains `load_balance` (signature invariant)

**Acceptance**:
- [x] Module + integration tests written and failing

### T17.9 — Implement LoadBalancerModule (TDD GREEN)
- [x] Constructor: parse module config dict (`.get()` + `validate_config_keys()`, matching every other module -- see T17.1 deviation note), bind endpoint adapters + pool_id, build `_PoolState`
- [x] `invoke()`: `select_endpoint()` → `arcllm.load_balance` span → chosen adapter `invoke()` → record health
- [x] `close()` closes every endpoint adapter (release all httpx pools)
- [x] `validate_config()` delegates across pool
- [x] Config validation at construction (strategy name, thresholds)

**Acceptance**:
- [x] All T17.8 module tests pass
- [x] `close()` releases all endpoint connection pools
- [x] Coverage >= 90% (achieved 100%)

### T17.10 — Wire registry.py + exports
- [x] Add `load_balance: bool | dict | None = None` kwarg to `load_model()`
- [x] Add `"load_balance"` to `MODULE_NAMES`
- [x] Innermost placement: when pool present + enabled, build endpoint adapters via `_build_adapter_for_endpoint` and construct `LoadBalancerModule` in the adapter/router slot
- [x] `_build_adapter_for_endpoint`: clone `ProviderConfig` with endpoint `base_url`/key-source, reuse `_build_adapter` vault path
- [x] Export `LoadBalancerModule`, `PoolExhaustedError` from `__init__.py`; update `modules/__init__.py`
- [x] Update `load_model` docstring stacking-order note to include LoadBalancer at innermost

**Acceptance**:
- [x] `load_model("openai", load_balance=True)` distributes across the TOML pool
- [x] No pool → identical single-adapter behavior (regression)
- [x] `test_registry.py` signature invariant passes
- [x] All existing tests pass (no regressions)

---

## Phase 6: Verification (Task 11)

### T17.11 — Full gate + docs
- [x] `pytest --cov=arcllm` — all pass, >= 90% on new files (load_balancer.py: 100%)
- [x] `mypy --strict` clean, `ruff check` clean
- [x] Verify OTel span emitted on a real balanced invoke (`TestLoadBalancerOtelSpan`)
- [x] Update spec README Learnings; update decision log D-449 → D-458
- [x] Confirm no arcrun edits (boundary held)

**Acceptance**:
- [x] All quality gates green
- [x] Decision log records D-449 through D-458
- [x] Cross-agent scheduling untouched (arcrun boundary preserved)

---

## Completion Checklist

- [x] All 11 tasks complete
- [x] All tests pass (existing + 66 new: 53 unit in test_load_balancer.py + 7 concurrency + 9 config + 8 registry - see report for exact split)
- [x] Coverage >= 90% on `modules/load_balancer.py` (achieved 100%)
- [x] Interleaving concurrency test proves shared-cursor safety
- [x] `mypy --strict` + `ruff check` clean
- [x] Decision log updated (D-449 through D-458)
- [x] Boundary respected: no cross-agent scheduling in arcllm
