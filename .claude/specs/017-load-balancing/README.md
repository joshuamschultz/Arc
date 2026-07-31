# Spec 017 — Load Balancing (Intra-Provider Endpoint/Key Distribution)

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 017 |
| Feature | Load Balancing (Intra-Provider Endpoint/Key Distribution) |
| Status | IMPLEMENTED |
| Created | 2026-07-05 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, user stories |
| [SDD.md](SDD.md) | Design, components, ADRs, edge cases |
| [PLAN.md](PLAN.md) | Phased tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-449 | Scope is **intra-provider**: spread calls across N endpoints/keys of the *same* provider | Distinct from FallbackModule (inter-provider failover) and RoutingModule (classification-based provider selection). Load balancing raises aggregate throughput of one provider; it never changes which provider or model answers. |
| D-450 | Default strategy is **weighted round-robin** | Deterministic, stateless-per-call, zero external deps. Weights let operators bias larger replicas or higher-quota keys. Simplest strategy that spreads load evenly — Simplicity pillar. |
| D-451 | **Health-aware** strategy skips endpoints via a per-endpoint circuit mechanism owned by the pool | Reuses circuit_breaker.py's failure-count/cooldown state machine *per endpoint* (not the per-provider CircuitBreakerModule). A tripped replica is skipped until cooldown; the pool degrades gracefully instead of hammering a dead endpoint. |
| D-452 | **Sticky routing** (by session/agent key) is optional, off by default | Pins a caller to one endpoint for prompt-cache locality (Anthropic/vLLM prefix cache hits). Off by default because it trades even distribution for cache warmth — an explicit operator choice. |
| D-453 | Pool topology (`[[endpoints]]`) lives in the **provider TOML**; strategy/behavior lives in `[modules.load_balance]` | Endpoints *are* provider connection settings (base_url, api_key_env/vault_path, weight) — they belong with the provider, resolved by the same ProviderSettings machinery. Behavior (strategy, sticky, health thresholds) is module config. Mirrors D-098 (global `[vault]` + per-provider `vault_path`). |
| D-454 | Cursor + per-endpoint health live in a **shared per-pool registry** guarded by `asyncio.Lock` | Mirrors rate_limit.py's `_bucket_registry`. Thousands of concurrent agents sharing one pool must advance a single round-robin cursor and read one health view — no singleton bottleneck, no per-agent drift. |
| D-455 | Cross-**agent** scheduling / fairness / global work-queue prioritization is **OUT OF SCOPE** | That is loop/runtime concern, not an LLM-call concern. It belongs to arcrun (future spec). arcllm balances *one caller's* invoke across endpoints; it never arbitrates *between* agents. |
| D-456 | **Per-call opt-in** via `load_model(..., load_balance=True | {dict})` | Consistent with every other optional module. Zero cost, zero imports, single-endpoint behavior unchanged when the kwarg is absent — zero-dep-when-disabled. |
| D-457 | Each endpoint's key resolves through the **same vault/env path** as the base provider | No plaintext keys in TOML or pool state. An endpoint declares `api_key_env` or `vault_path`; resolution reuses `_build_adapter` + VaultResolver + BaseAdapter. Health data holds counters/timestamps only — never message content. |
| D-458 | LB sits at the **innermost** stack position, holding a pool of endpoint adapters (Router-like) | The adapter must target the chosen endpoint's base_url/key, so selection happens closest to the wire. RateLimit/Retry/CircuitBreaker wrap the pool; Queue still caps concurrency above it. LB replaces the single adapter, exactly as RoutingModule does. |

## Cross-References

- Prior decisions: D-098 (global `[vault]` config + per-provider `vault_path` — pool config location precedent), spec 006 (RoutingModule pool-of-adapters pattern), spec 008 (rate_limit.py shared per-provider registry pattern)
- Related specs: [007-module-system-retry-fallback](../007-module-system-retry-fallback/) (FallbackModule is inter-provider — LB is intra-provider), [008-rate-limiter](../008-rate-limiter/) (shared-registry concurrency pattern reused), circuit_breaker (per-endpoint health mechanism reused)
- Build standards: `packages/arcllm/CLAUDE.md` — Scalability ("1,000s of agents concurrently", "No singleton bottlenecks", "Connection pooling"), Separation of concerns ("all llm calls are arcllm … loop execution is arcrun")

## Learnings

- **`LoadBalanceModuleConfig` typed subclass was not built.** Every other module
  (`rate_limit`, `circuit_breaker`, `retry`, `fallback`, `telemetry`, `queue`, `audit`,
  `security`, `otel`) parses `config: dict[str, Any]` inline with `.get()` defaults +
  `validate_config_keys()` — no per-module typed Pydantic config class exists anywhere.
  `LoadBalancerModule` follows that convention for consistency (YAGNI); `EndpointConfig`
  *is* a typed Pydantic model since it's genuinely new provider-TOML structure, matching
  `ModelMetadata`/`ProviderSettings`.
- **`LoadBalancerModule` subclasses `LLMProvider` directly, not `BaseModule`.** `BaseModule`
  assumes a single `inner: LLMProvider`; a pool-holding module needs `RoutingModule`'s shape
  instead (which also does not subclass `BaseModule`). The SDD's own Architecture Fit section
  and ADR-7 already say "exactly like RoutingModule" / "reuses the RoutingModule pattern" —
  this resolves an internal SDD wording inconsistency rather than departing from its intent.
- **A literal "remove the lock and watch the test fail" is not achievable for this design.**
  `_select_weighted_rr`/`_select_health_aware`/`_select_sticky` contain zero `await` inside
  their locked sections (D-454's own discipline). Under asyncio's cooperative single-threaded
  scheduler, a block with no internal `await` cannot be preempted regardless of locking — there
  is no yield point to race on. Proved the concurrency-safety property instead via (a) an
  instrumented-lock subclass showing the lock is genuinely acquired exactly once per call under
  `asyncio.Barrier`-forced contention, and (b) a companion test reproducing the classic
  unlocked-read-modify-write-with-await anti-pattern under the identical harness, showing it
  *does* corrupt state — proving the Barrier technique would catch a real regression.
- **Freezing `time.monotonic()` globally hangs real `asyncio.sleep`.** The recovery
  thundering-herd concurrency test originally froze `time.monotonic` (as the synchronous
  `_EndpointHealth` unit tests safely do) while also using a genuine `await asyncio.sleep(...)`
  to keep a probe in-flight. Since asyncio's default event loop uses `time.monotonic()` for its
  own clock (`loop.time()`), freezing it hangs every real sleep in the process. Fixed by using a
  tiny real `cooldown_seconds` + a tiny real `asyncio.sleep` to let it elapse, only mocking
  `random.uniform` for jitter determinism.
- **Recovery-herd test needed artificial probe latency to be meaningful.** With a zero-latency
  mock, the first concurrent prober's success closes the circuit before any sibling task even
  runs (asyncio never preempts a coroutine with no internal `await`), trivially "avoiding" the
  herd risk the guard exists for. Giving the recovering endpoint's mock adapter real latency
  (while the already-healthy endpoint resolves instantly) exposes genuine overlapping in-flight
  probes and correctly demonstrates the `half_open_max_calls` cap.
- **`ruff format .` run on the whole repo incidentally reformatted one pre-existing long line**
  in `tests/test_prompt_caching.py` (unrelated to this spec). Left in per CLAUDE.md's
  "leave it correct" mandate rather than reverting.
