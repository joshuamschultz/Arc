# SDD — Load Balancing (Intra-Provider Endpoint/Key Distribution)

## Design Overview

Spec 017 adds one optional module, `LoadBalancerModule`, that distributes a single
caller's `invoke()` calls across a **pool of equivalent endpoints of the same
provider** — N vLLM/Ollama replicas serving one model, or N API keys for one
provider that together raise the aggregate rate limit.

It is deliberately **intra-provider**. It never changes which provider or model
answers (that is FallbackModule and RoutingModule, respectively). It only chooses
*which endpoint variant* of the already-selected provider the adapter targets.

The module holds a **pool of endpoint adapters** and, per invoke, selects one via a
configurable strategy:

1. **Weighted round-robin** (default) — advance a shared per-pool cursor by weight.
2. **Health-aware** — skip endpoints whose per-endpoint circuit is OPEN; probe on cooldown.
3. **Sticky** (optional) — pin a session/agent key to one endpoint for prompt-cache locality.

The cursor and per-endpoint health live in a **shared per-pool registry** guarded by
`asyncio.Lock`, mirroring `rate_limit.py`'s `_bucket_registry`. One cursor and one
health view per pool, regardless of how many agents share it — no singleton
bottleneck, no per-agent drift.

### Architecture Fit

```
Agent: load_model("openai", load_balance=True)
  │
  └── Build module stack (outermost → innermost, per registry.py):
      Otel → Queue → Telemetry → Audit → Security → CircuitBreaker
              → Retry → Fallback → RateLimit → LoadBalancer(pool)
                                                     │
                                                     │  per invoke():
                                                     ▼
                          ┌───────────── LoadBalancerModule ──────────────┐
                          │  strategy = weighted_rr | health_aware | sticky │
                          │                                                 │
                          │  shared per-pool registry (asyncio.Lock):       │
                          │    cursor: int      health: {ep_id: CircuitState}│
                          │                                                 │
                          │  select_endpoint() ──► endpoint variant #k      │
                          └───────────────────────┬─────────────────────────┘
                                                  │
                    ┌──────────────┬──────────────┼──────────────┐
                    ▼              ▼              ▼              ▼
              endpoint #0    endpoint #1    endpoint #2    endpoint #k …
              base_url A     base_url B     base_url C
              key env_A      vault path_B   key env_C
              weight 2       weight 1       weight 1
                    │              │              │
              BaseAdapter    BaseAdapter    BaseAdapter   (one httpx pool each)
                    │              │              │
                    └──────────────┴──── provider wire ───┘
```

**Key placement facts:**
- LB is **innermost** — it replaces the single adapter, exactly as RoutingModule does (D-458). Selection happens closest to the wire because each endpoint has its own `base_url`/key.
- **RateLimit** stays *outside* LB (per-provider aggregate cap). **Queue** stays outermost-ish (concurrency cap). Both function unchanged — LB is complementary, not a replacement.
- Each endpoint is a full `BaseAdapter` with its **own httpx connection pool** (D-458, NFR-5) — built once, reused across invokes.

## Directory Map

### New Files

```
src/arcllm/
└── modules/
    └── load_balancer.py     # LoadBalancerModule + strategies + shared per-pool registry
                             # + PoolExhaustedError + _EndpointHealth (per-endpoint circuit)
```

### Modified Files

```
src/arcllm/
├── config.py                # EndpointConfig model; endpoints: list[EndpointConfig] on ProviderConfig;
│                            #   LoadBalanceModuleConfig fields
├── config.toml              # [modules.load_balance] section (strategy, sticky, health thresholds)
├── providers/openai.toml    # example [[endpoints]] array (commented template)
├── providers/vllm.toml      # example [[endpoints]] pool for self-hosted replicas (if present)
├── registry.py              # load_balance kwarg; MODULE_NAMES += "load_balance";
│                            #   build pool of endpoint adapters; innermost placement
└── __init__.py              # export LoadBalancerModule, PoolExhaustedError
```

### New Test Files

```
tests/
├── test_load_balancer.py            # strategy selection, weighting, health, sticky, edge cases
└── test_load_balancer_concurrency.py # barrier-forced interleaving proves shared cursor safety
```

## Pool Config Design — where the pool lives (D-453)

Two candidate locations were evaluated:

| Option | Where | Pros | Cons |
|--------|-------|------|------|
| **A — provider TOML `[[endpoints]]`** ✅ chosen | `providers/<name>.toml` | Endpoints *are* connection settings (base_url/key/weight); reuse `ProviderSettings` validation (HTTPS enforcement, key resolution); topology is a property of the provider, versioned with it | Provider TOML grows an array |
| B — `[modules.load_balancer]` pool | `config.toml` | Keeps all module config in one place | Duplicates connection fields already modeled by `ProviderSettings`; splits a provider's wire topology away from the provider; second HTTPS/key-resolution code path |

**Decision:** the **pool topology lives in the provider TOML** as an `[[endpoints]]`
array; the **strategy/behavior lives in `[modules.load_balance]`** (and the per-call
kwarg). This mirrors D-098 from spec 012 (global `[vault]` connection config +
per-provider `vault_path`): *connection topology is per-provider, behavior is
per-module.* It also means a pool endpoint reuses the exact same secure key
resolution as the base provider (D-457) — no second path to audit.

### Provider TOML — `[[endpoints]]`

```toml
# providers/openai.toml
[provider]
api_format = "openai-chat"
base_url = "https://api.openai.com"     # default single endpoint (used when pool empty)
api_key_env = "OPENAI_API_KEY"
api_key_required = true
default_model = "gpt-4.1"
default_temperature = 0.7
vault_path = ""

# Optional endpoint pool. When present AND load_balance is enabled, invokes are
# distributed across these. When absent, behavior is exactly as today.
[[endpoints]]
base_url = "https://api.openai.com"
api_key_env = "OPENAI_API_KEY_A"
weight = 2

[[endpoints]]
base_url = "https://api.openai.com"
api_key_env = "OPENAI_API_KEY_B"
weight = 1

[[endpoints]]
base_url = "https://api.openai.com"
vault_path = "secret/openai/key-c"        # vault instead of env — same resolver
weight = 1
```

### Config Models (`config.py` additions)

```
Class: EndpointConfig(BaseModel)
  Fields:
    base_url: str                       # reuses ProviderSettings HTTPS validator
    api_key_env: str = ""               # env var name for this endpoint's key
    vault_path: str = ""                # OR vault path (mutually complementary)
    weight: int = 1                     # >= 0; 0 = drained (never selected)
  Validators:
    - base_url: HTTPS-for-remote (shared with ProviderSettings)
    - weight >= 0
    - at least one of api_key_env / vault_path when api_key_required

Modified: ProviderConfig
    endpoints: list[EndpointConfig] = []   # empty = single-endpoint (today's behavior)

Class: LoadBalanceModuleConfig(ModuleConfig)   # extra="allow", enabled: bool
  Fields:
    strategy: str = "weighted_round_robin"     # | "health_aware" | "sticky"
    sticky_key: str = "session_id"             # kwarg name read for the sticky pin
    failure_threshold: int = 5                 # per-endpoint circuit (health_aware/sticky)
    cooldown_seconds: float = 30.0
    half_open_max_calls: int = 1
```

## The Three Strategies

### 1. Weighted Round-Robin (default)

Deterministic, stateless per call except for the shared cursor. Endpoints are
expanded into a weighted selection sequence (an endpoint with `weight=2` appears
twice as often). Each invoke advances the shared cursor under the lock and returns
the endpoint at that slot. `weight=0` endpoints are excluded from the sequence
(drained).

```
select_weighted_rr(pool) -> Endpoint:
    async with registry.lock(pool_id):
        idx = registry.cursor(pool_id)
        registry.set_cursor(pool_id, (idx + 1) % len(weighted_sequence))
    return weighted_sequence[idx]        # read outside the lock
```

### Research Insights — Weighted Round-Robin & Usage-Aware Deferral

- **Reference designs validate the default.** LiteLLM's default `simple-shuffle` is weight-aware random; Portkey uses **weighted random selection** normalizing weights to 100% (weights 5/3/1 → 55/33/11%). Both ship weighted distribution as the baseline and treat latency/least-busy as opt-in upgrades — matching ADR-2 (default weighted RR, defer least-connections/latency). [litellm routing](https://docs.litellm.ai/docs/routing), [Portkey load balancing](https://portkey.ai/docs/product/ai-gateway/load-balancing)
- **Deterministic RR beats weighted-random at low volume.** Our cursor-based expansion (weight=2 → two slots) gives exact proportions even over a handful of calls, whereas weighted-random only converges in expectation. Right call for agents making few invokes per session — keep it.
- **Usage-aware routing is the real future upgrade, not latency.** LiteLLM's `usage-based`/`least-busy` routes by each deployment's rpm/tpm headroom — directly relevant because our pool endpoints often *are* separate API keys with separate TPM quotas. A latency-EWMA strategy needs live metrics; a usage-aware strategy only needs the per-endpoint RateLimit bucket state we already track. Note as a future strategy; keep out of scope (ADR-2 holds). [litellm routing-load-balancing](https://docs.litellm.ai/docs/routing-load-balancing)
- **Cooldown-on-429 is a documented gateway pattern.** Portkey/Cloudflare AI Gateway cool an endpoint on a 429 (honoring `Retry-After`) rather than immediately re-selecting it. Weighted-RR (non-health) surfaces the 429 to Retry/RateLimit above unchanged; health-aware mode should count a 429 toward the per-endpoint circuit (see Health-Aware insights). [Cloudflare AI Gateway rate limiting](https://developers.cloudflare.com/ai-gateway/features/rate-limiting/)
- (a) **Security:** deterministic selection is not attacker-influenced (cursor is internal integer state, no caller input); no endpoint identity leaks on the RR path. (b) **Scalability:** O(1) selection, single integer mutate under lock — see Shared-Registry insights for contention math. (c) **Boundary:** weighted RR never inspects data class (Routing) or fails over providers (Fallback); it only advances a cursor within one provider's pool.

### 2. Health-Aware

Wraps weighted RR with a per-endpoint circuit. Before returning an endpoint, check
its `_EndpointHealth`; if OPEN and still within cooldown, advance past it. On invoke
failure, record the failure against that endpoint's health; on success, reset. If
every endpoint is OPEN, raise `PoolExhaustedError` (never hang).

`_EndpointHealth` reuses the exact CLOSED → OPEN → HALF_OPEN → CLOSED semantics of
`circuit_breaker.py` (failure_threshold, cooldown_seconds, half_open_max_calls) —
but **per endpoint**, owned by the pool, distinct from the per-provider
`CircuitBreakerModule`.

### Research Insights — Health-Aware Ejection & Recovery

- **Envoy's outlier detection is the canonical model and confirms our defaults.** Envoy ejects on **5 consecutive 5xx** (our `failure_threshold=5` matches) and un-ejects after `base_ejection_time × times_ejected` — an **escalating** cooldown for repeat offenders, not a flat window. Consider growing `_EndpointHealth` cooldown with consecutive-ejection count (backoff) so a flapping endpoint is quarantined longer than a one-off failure. [Envoy outlier detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier.html)
- **Thundering-herd on recovery is a real failure mode — add jitter.** Envoy adds ejection-time **jitter** to stagger when hosts return, preventing all proxies re-hitting a just-recovered host simultaneously. With thousands of agents sharing one `_PoolState`, the instant an endpoint's cooldown lapses every waiting agent could stampede it. HALF_OPEN admitting only `half_open_max_calls` (=1) probe is our primary herd guard; additionally add small random jitter (±10–20%) to effective cooldown so pooled agents don't all transition to HALF_OPEN on the same tick. **Implies a recovery-thundering-herd test.** [Envoy outlier detection](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier.html)
- **Panic threshold — don't independently eject the whole pool.** Envoy caps ejection at `max_ejection_percent` (default 10%) and enters panic mode when too many hosts are unhealthy, because a *correlated* failure (bad upstream, network) shouldn't remove all capacity. Our all-OPEN → `PoolExhaustedError` is correct fail-closed behavior for a single caller, but a provider-wide failure should trip the per-provider CircuitBreakerModule wrapping the pool, not N endpoint circuits redundantly. Document: correlated failure → provider CB; isolated failure → endpoint circuit. [Envoy outlier detection brownouts](https://www.michal-drozd.com/en/blog/envoy-outlier-detection-brownouts/)
- **Passive over active health.** Envoy's outlier detection is *passive* (learns from real request results) vs synthetic active probes. Passive fits arcllm: no cost budget to burn synthetic LLM calls, and real invoke results are a truer signal. Keep `_EndpointHealth` passive; HALF_OPEN reuses the *next real invoke* as the probe (no synthetic call). State this in the module docstring.
- (a) **Security:** health state stores only counters + timestamps + endpoint id (base_url/key-source hash), never prompt/response content (ADR-8) — satisfies "telemetry holds no sensitive content." Skipping a dead/rate-limited key denies an attacker the ability to pin traffic onto one exhausted key (LLM10). (b) **Scalability:** health mutation is O(1) under the same per-pool lock; escalating cooldown evicts flappers longer, reducing repeated lock churn from retries. (c) **Boundary:** per-endpoint circuit is strictly finer-grained than CircuitBreakerModule (per-provider) and Fallback (inter-provider) — LB never trips or switches the provider.

### 3. Sticky (optional)

Reads a caller identity from kwargs (`sticky_key`, default `session_id`) and pins it
to an endpoint via a stable hash over the weighted sequence, so repeated calls from
one agent hit the same endpoint for prompt-cache locality. If the pinned endpoint is
unhealthy, fall back to health-aware weighted RR and re-pin (eviction). No pin state
is stored per session (stateless hash) — avoids unbounded memory across sessions.

### Research Insights — Sticky Routing & Prompt-Cache Locality

- **The prize is large and measured.** KubeAI's consistent-hashing-with-bounded-loads (CHWBL) for LLM routing reports **95% lower time-to-first-token** and **+127% throughput** vs random routing at 1200 concurrent threads, purely from prefix/KV-cache hits. vLLM session-aware routing reaches a **96.26% cache hit rate**. This justifies sticky as a first-class opt-in strategy. [KubeAI CHWBL](https://www.kubeai.org/blog/2025/02/26/llm-load-balancing-at-scale-chwbl/), [vLLM prefix-aware routing](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html)
- **Bounded-load consistent hashing solves the exact tension we flagged.** Google's CHWBL (arXiv 1608.01350, used in Cloud Pub/Sub) pins a key to its hashed endpoint **until that endpoint hits a load cap, then overflows to the next ring position** — guaranteeing max-load ≤ (1+ε)·average while preserving stickiness. Our design pins via stable hash and *evicts only on health failure*; it does **not** overflow on load. **Owner decision:** a hot session key (one very busy agent) will hammer one endpoint with no relief until it goes unhealthy. If even distribution under skew matters, adopt bounded-load overflow (skip to next slot when the pinned endpoint's load exceeds a cap) — but that requires in-flight tracking (more state). Recommend documenting the limitation now and deferring CHWBL to a future strategy unless skew is observed. [Consistent Hashing with Bounded Loads](https://arxiv.org/abs/1608.01350), [Google Research](https://research.google/blog/consistent-hashing-with-bounded-loads/)
- **Prefix-aware vs KV-cache-aware.** vLLM distinguishes *prefix-aware* (always same instance for a prefix, even if cache evicted) from *KV-cache-aware* (routes to whoever still holds the cache). Our stateless-hash sticky is prefix-aware-equivalent — the cheaper, simpler variant. Correct: KV-cache-aware needs live cache introspection arcllm can't see across opaque providers. [vLLM KV-cache aware routing](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html)
- **Stateless hash is the right memory posture.** CHWBL and vLLM keep a ring, not a per-session map; our stateless `hash(sticky_key) % len(weighted_sequence)` stores zero per-session state — critical at thousands of agents (ADR-4), confirmed by the reference designs' avoidance of unbounded session tables.
- (a) **Security:** `sticky_key` (session/agent id) is hashed, never logged raw; a caller cannot force a specific endpoint without knowing the full weighted-sequence layout, and even then only affects its *own* traffic — no cross-tenant steering (ASI03). Normalize `sticky_key` with NFKC if it can carry user-adjacent text. (b) **Scalability:** O(1) stateless hash, no memory growth with session count — the key scalability property; skew risk noted above. (c) **Boundary:** stickiness is intra-pool cache locality only; never changes provider/model, never coordinates across agents (arcrun).

## Shared-Registry Concurrency Design

Mirrors `rate_limit.py`'s module-level `_bucket_registry`, but per **pool** instead
of per provider.

```
# module-level, one entry per distinct pool
_pool_registry: dict[str, _PoolState] = {}

class _PoolState:
    cursor: int
    health: dict[str, _EndpointHealth]   # ep_id -> per-endpoint circuit
    lock: asyncio.Lock

def _get_or_create_pool(pool_id, endpoints) -> _PoolState: ...
def clear_pools() -> None: ...           # test isolation, called from registry.clear_cache()
```

- **pool_id** is a stable hash of the provider name + endpoint identities (base_url + key-source), so two `LoadBalancerModule` instances over the same pool share one cursor and one health view (FR-6).
- The `asyncio.Lock` is held **only around the cursor increment / health mutation** — the actual `await inner.invoke()` happens *outside* the lock so concurrent callers are not serialized (same discipline as `TokenBucket.acquire()`, which sleeps outside its lock).
- **Concurrency invariant (SC-8 / FR-7):** under interleaving, the cursor issues each slot exactly once — no two concurrent callers read the same cursor value, and none is skipped. The concurrency test forces true interleaving with an `asyncio.Barrier`/`Event` (an instant mock would let `gather` run tasks sequentially and pass even with an unsafe cursor).

### Research Insights — Concurrency-Safe Shared Cursor at Fleet Scale

- **The mutate-under-lock / await-outside-lock discipline is proven in-repo.** `rate_limit.py::TokenBucket.acquire()` holds its `asyncio.Lock` only around `_refill()` + token decrement, then `await asyncio.sleep()` **outside** the lock (lines 44–55). The solutions archive reinforces this: "Observer Callback Outside Lock" and the async-scheduler hardening both hold locks only around state mutation, never around `await`. `select_endpoint()` must increment cursor / read health under the lock and `await inner.invoke()` outside. [rate_limit.py; solutions/2026-02-21-arcrun-phase4-hardening]
- **Quantify the contention ceiling.** The critical section is a single integer increment + modulo (plus, for health-aware, a dict lookup) — order **hundreds of nanoseconds**. Under asyncio's single-thread loop the lock is uncontended in the common case; even at thousands of agents sharing one `_PoolState`, lock hold ≈ 100s ns while `invoke()` latency ≈ 100s of **ms** — a ~10⁶× ratio. The shared cursor is therefore **not** a singleton bottleneck: throughput is bounded by provider RTT, not the lock. The anti-pattern (holding the lock across `await invoke()`) would serialize the fleet to one-in-flight-at-a-time — the exact "singleton bottleneck" CLAUDE.md forbids. **Implies a contention micro-benchmark** asserting sub-microsecond lock hold and that N concurrent selects complete in ~max(invoke latency), not sum.
- **Re-read shared state, don't trust a stale local copy.** The scheduler-hardening learning ("re-read from store for latest state") maps directly: health-aware selection must read `_PoolState.health[ep_id]` *inside* the lock at decision time, not cache a snapshot from an earlier call, or concurrent failures produce lost updates. The concurrency test's "health recorded exactly once per failure" case guards this. [solutions/2026-02-16-async-scheduler-hardening]
- **Force interleaving or the test lies.** Per the standing memory ("concurrency tests must force interleaving") and the async-scheduler review, an instant mock lets `asyncio.gather` run tasks sequentially — the cursor test passes even with the lock removed. The `asyncio.Barrier`/`Event` gate in T17.7 is mandatory, and the test must also **fail with the lock removed** to prove it catches the unsafe path (already in PLAN acceptance — keep it).
- **Bound any per-pool collections.** The runtime-hardening tech-debt note ("unbounded collections… eventually consume all memory") applies only if `health` ever keys by caller-supplied data. It is keyed by fixed endpoint ids (pool size is static config) → inherently bounded; confirm no per-session/per-request keys ever enter `_PoolState`.
- (a) **Security:** the lock prevents a race that could skip a health check and route to a dead/exhausted endpoint (availability, LLM10). Registry keyed by endpoint identity hash, not secrets. (b) **Scalability:** THE scalability core of this feature — one cursor+health per pool, O(1) sub-µs critical section, no per-agent drift, no fleet serialization. (c) **Boundary:** the shared registry coordinates one caller's own concurrent invokes; it never arbitrates *between* agents — cross-agent fair scheduling is arcrun (D-455).

## Registry Integration (`registry.py`)

```
MODULE_NAMES += "load_balance"                     # preserves test_registry signature invariant

# In load_model(), innermost (replaces single adapter, like routing):
lb_config = _resolve_module_config("load_balance", load_balance)
if lb_config is not None and config.provider... endpoints:      # pool present
    from arcllm.modules.load_balancer import LoadBalancerModule
    endpoint_adapters = [
        _build_adapter_for_endpoint(provider, model_name, ep, vault_cfg, resolver)
        for ep in config.endpoints if ep.weight > 0
    ]
    result = LoadBalancerModule(lb_config, endpoint_adapters, pool_id=...)
else:
    result = _build_adapter(provider, model_name, vault_cfg, resolver)   # today's path

# Then RateLimit, Fallback, Retry, CircuitBreaker, Security, Audit, Telemetry, Queue, Otel
# wrap result exactly as today.
```

`_build_adapter_for_endpoint` clones the resolved `ProviderConfig` with the
endpoint's `base_url` and key-source overridden, then reuses `_build_adapter`'s vault
resolution + `BaseAdapter` construction (D-457). No new key-handling code.

## Interplay & Boundaries

| Module | Concern | Relationship to LB | Boundary |
|--------|---------|--------------------|----------|
| **FallbackModule** (007) | Inter-provider failover *after* failure | Different axis. LB spreads within one provider; Fallback switches providers on total failure. Can stack: LB inner, Fallback outer. | LB never switches provider or model. |
| **RoutingModule** (006) | Classification → provider/model selection | Different axis. Router picks *which* provider by data class; LB picks *which endpoint* of the chosen provider. Both are innermost-replacers → use one or the other per provider. | LB never inspects classification. |
| **CircuitBreakerModule** | Per-**provider** health, trips whole provider | LB keeps its own per-**endpoint** health (finer grain). Provider-level CB still wraps the pool and trips if the *whole* pool fails. | LB owns per-endpoint state; CB owns per-provider state. |
| **QueueModule** | Bounded concurrency + backpressure | Complementary. Queue caps how many invokes run at once; LB decides where each runs. | LB does not cap concurrency. |
| **RateLimitModule** (008) | Per-key token bucket | Complementary. With multiple keys, each endpoint can carry its own quota; LB spreads to raise aggregate. RateLimit still wraps the pool for a global cap. | LB does not throttle. |
| **arcrun** | Cross-**agent** scheduling / fairness | Out of scope (see below). | LB balances one caller's invokes only. |

### Boundaries — arcrun owns cross-agent scheduling (D-455)

Load balancing here is strictly **one caller distributing its own invokes across
equivalent endpoints of one provider.** It does **not**, and must never:

- arbitrate fairness *between* different agents,
- prioritize one agent's work-queue over another's,
- schedule or throttle at the fleet level,
- coordinate a global work queue.

Those are **loop/runtime** concerns and belong to **arcrun** (future spec). Per
`packages/arcllm/CLAUDE.md`: *"all llm calls are arcllm … loop execution is
arcrun."* Cross-agent scheduling is loop execution. Keeping it out of arcllm
preserves shared-nothing-per-agent: an agent's LB state is about its provider's
endpoints, never about other agents.

### Research Insights — Module Boundaries & Availability Posture

- **Every reference gateway keeps these axes separate — validating our three-module split.** LiteLLM/Portkey model *fallbacks* (provider-on-failure), *routing strategies* (which deployment), and *load balancing* (spread across equivalent targets) as distinct config surfaces, not one knob. Our Fallback (inter-provider) / Routing (by class) / LoadBalancer (intra-provider, innermost) separation mirrors industry practice. [LLM router comparison 2026](https://www.developersdigest.tech/blog/llm-router-comparison-2026)
- **NIST SC-5(2) names load balancing as a DoS control.** SC-5(2) "Capacity, Bandwidth, and Redundancy" explicitly lists *load balancing* alongside quotas and partitioning to "limit the effects of flooding denial-of-service attacks." Distributing across N endpoints/keys is a direct SC-5 implementation: no single key/replica is a chokepoint, and a flood against one endpoint degrades ⅟N of capacity, not all. Cite SC-5(2) in the module docstring for the federal-compliance trail. [NIST SC-5(2)](https://csf.tools/reference/nist-sp-800-53/r5/sc/sc-5/sc-5-2/)
- **LLM10 (Unbounded Consumption) cuts both ways.** LB *raises* aggregate throughput (more keys → more TPM), which could mask a runaway loop. The per-endpoint RateLimit buckets and the outer Queue/CircuitBreaker remain the consumption ceiling; LB must never *bypass* them — it distributes *under* the aggregate cap, it does not lift it. Confirm RateLimit stays outside LB in the stack (it does, per Architecture Fit) so the global cap is enforced above distribution.
- (a) **Security:** availability-by-distribution (SC-5), no single-key chokepoint; LB additions store no secrets/content. (b) **Scalability:** aggregate TPM scales with pool size; the cap is enforced by RateLimit above, not weakened by LB. (c) **Boundary:** the crisp rule — LB spreads one caller within one provider; Fallback switches providers; Routing picks by class; arcrun schedules across agents. Four axes, four owners.

## ADRs

### ADR-1: Intra-Provider Scope Only (D-449)

**Context:** Three superficially similar modules could all "pick where a call goes": Fallback (provider on failure), Routing (provider by classification), Load Balancing (endpoint by load).

**Decision:** LoadBalancerModule is strictly intra-provider — it selects among equivalent endpoints/keys of the *already-chosen* provider and model. It never changes provider or model.

**Rationale:** Clean separation of concerns (CLAUDE.md). Overlap would create ambiguous config where two modules fight over the innermost slot. Each module owns exactly one axis: provider-on-failure, provider-by-class, endpoint-by-load.

**Alternatives rejected:** A unified "traffic director" doing all three (conflates three orthogonal decisions, violates Simplicity/Modularity).

### ADR-2: Weighted Round-Robin Default (D-450)

**Context:** Need a default strategy that spreads load with zero external deps and predictable behavior.

**Decision:** Weighted round-robin via a shared per-pool cursor. Weights bias larger replicas / higher-quota keys.

**Rationale:** Deterministic, testable, O(1) selection, no dependency. Least-connections / latency-aware strategies need live metrics and add complexity — deferred.

**Alternatives rejected:** Random (harder to reason about, uneven at low volume); least-connections (needs in-flight tracking, future enhancement).

### ADR-3: Health-Aware via Per-Endpoint Circuit (D-451)

**Context:** A dead replica must not keep receiving a share of traffic.

**Decision:** The pool owns a per-endpoint circuit (`_EndpointHealth`) reusing circuit_breaker.py's CLOSED/OPEN/HALF_OPEN semantics. Health-aware strategy skips OPEN endpoints and probes on cooldown.

**Rationale:** Reuses a proven, tested state machine at a finer grain. The per-provider CircuitBreakerModule cannot express "endpoint 2 of 4 is down" — it only knows the whole provider.

**Alternatives rejected:** Reuse CircuitBreakerModule directly (wrong granularity — it is per-provider); passive removal with no recovery (endpoints never come back).

### ADR-4: Optional Sticky Routing (D-452)

**Context:** Prompt caching (Anthropic / vLLM prefix cache) rewards hitting the same endpoint repeatedly, but that fights even distribution.

**Decision:** Sticky is opt-in (`strategy = "sticky"`), pins a caller key via a stable hash, and evicts to health-aware RR when the pinned endpoint is unhealthy. Stateless hash — no per-session memory.

**Rationale:** Cache locality is a real cost/latency win but an explicit tradeoff. Default stays even (RR). Stateless hashing avoids unbounded session maps across thousands of agents.

**Alternatives rejected:** Sticky by default (silently sacrifices distribution); stateful session→endpoint map (unbounded memory, singleton-ish).

### ADR-5: Pool in Provider TOML, Behavior in Module Config (D-453)

**Context:** Where does the endpoint list live — provider TOML or module config?

**Decision:** `[[endpoints]]` in the provider TOML (topology); `[modules.load_balance]` for strategy/behavior. See "Pool Config Design" tradeoff table.

**Rationale:** Endpoints are connection settings and reuse `ProviderSettings` validation + secure key resolution. Mirrors D-098 (per-provider `vault_path` + global `[vault]`). Avoids a second connection-config code path.

**Alternatives rejected:** All-in-module pool (duplicates connection modeling, splits topology from provider).

### ADR-6: Shared Per-Pool Registry with asyncio.Lock (D-454)

**Context:** Thousands of agents may share one pool; the round-robin cursor and health must be single-source, not per-agent.

**Decision:** Module-level `_pool_registry` keyed by pool identity; `asyncio.Lock` guards cursor/health mutation only; `await invoke()` runs outside the lock. Mirrors rate_limit.py's `_bucket_registry`.

**Rationale:** One cursor + one health view per pool = correct distribution and correct health regardless of agent count. Lock scope kept minimal so callers are not serialized (async-first, no singleton bottleneck).

**Alternatives rejected:** Per-instance cursor (each agent restarts at 0 → skewed distribution); global lock around invoke (serializes the fleet).

### ADR-7: Innermost Stack Placement (D-458)

**Context:** The adapter must target the chosen endpoint's base_url/key, so selection must happen where the adapter is constructed.

**Decision:** LoadBalancerModule is innermost, holding a pool of endpoint adapters, replacing the single adapter (Router-like). RateLimit/Retry/CircuitBreaker/Queue wrap it unchanged.

**Rationale:** Selection closest to the wire. Reuses the RoutingModule pattern (holds a dict/list of adapters). Outer modules stay oblivious — they see one `LLMProvider`.

**Alternatives rejected:** LB swapping base_url on a single adapter per call (loses per-endpoint connection pooling; mutating a shared adapter is not shared-nothing-safe).

### ADR-8: Per-Endpoint Key Resolution Reuse (D-457)

**Context:** Each endpoint needs its own key without introducing plaintext secrets.

**Decision:** Endpoints declare `api_key_env` or `vault_path`; resolution reuses `_build_adapter` + VaultResolver + BaseAdapter. No literal-key field exists.

**Rationale:** Single audited key path (CLAUDE.md: "Credentials never touch the filesystem"). Health/cursor state stores counters and timestamps only — never message content.

**Alternatives rejected:** Inline `api_key` string in TOML (plaintext secret on disk — forbidden).

## Edge Cases

| Case | Handling |
|------|----------|
| No `[[endpoints]]` in provider TOML | Degenerate: `load_balance` no-ops, single adapter built as today (FR-15). |
| Single-endpoint pool | Pool of one; every call goes there. Cursor advances trivially. Still valid. |
| `load_balance=True` but empty pool | Treated as no pool → pass-through (log info: "load_balance enabled but no endpoints; using single provider"). |
| All endpoints unhealthy | Raise `PoolExhaustedError` immediately — never hang, never silently pick a dead endpoint (FR-11, SC-5). |
| `weight = 0` on an endpoint | Excluded from the weighted sequence (drained). If all weights are 0 → `ArcLLMConfigError` at construction (nothing selectable). |
| Endpoint fails mid-invoke (health-aware) | Record failure against that endpoint; try next healthy endpoint; if none, `PoolExhaustedError`. Weighted-RR (non-health) surfaces the error to Retry/Fallback above. |
| Sticky key not present in kwargs | Fall back to weighted RR for that call (no pin possible); log debug once. |
| Sticky pinned endpoint unhealthy | Evict: re-pin via health-aware RR to a healthy endpoint; original pin restored when it recovers (stateless hash re-selects it). |
| Key rotation mid-pool (env/vault changes) | Adapters are long-lived; vault keys refresh via VaultResolver TTL cache (spec 012) on next resolution. Env-var endpoints pick up new keys only on adapter rebuild (documented; same as base provider today). |
| Two agents, same pool | Share one `_PoolState` via `_pool_registry` (pool_id hash) — one cursor, one health view (FR-6). |
| Duplicate endpoints (same base_url+key) | Collapsed by pool_id/endpoint identity is per-slot; duplicates are allowed and simply get more weight-share. |
| Invalid endpoint config (bad base_url / missing key source) | `ArcLLMConfigError` at construction (HTTPS validator + key-source check), before any invoke. |
| Concurrency: two callers hit cursor simultaneously | Lock serializes the increment; each gets a distinct slot (SC-8). Verified by barrier-forced interleaving test. |

### Research Insights — Edge Cases from Reference Designs

- **Honor `Retry-After` on 429.** Cloudflare/Portkey honor the provider's `Retry-After` header on 429 rather than a fixed cooldown. In health-aware mode, a 429 from one endpoint should set that endpoint's cooldown to `max(cooldown_seconds, Retry-After)` so a rate-limited key isn't re-probed too early. Add handling + test. [Cloudflare AI Gateway rate limiting](https://developers.cloudflare.com/ai-gateway/features/rate-limiting/)
- **Correlated vs isolated all-unhealthy.** When *every* endpoint trips near-simultaneously (provider outage), `PoolExhaustedError` per the table is correct for the caller, but the wrapping per-provider CircuitBreaker should trip so the whole provider fast-fails rather than each agent independently exhausting its pool. Test: all-unhealthy surfaces `PoolExhaustedError` AND, under CB, the provider trips.
- **Weight=0 drain is an operational feature.** Envoy/gateways use weight=0 to drain a node for maintenance without removing config. Our `weight=0 → excluded, all-zero → ArcLLMConfigError` matches; add an explicit test that a weight=0 endpoint receives **zero** selections across many calls (not merely "excluded from sequence").
- **Recovery herd.** Per Health-Aware insights, when a pooled endpoint's cooldown lapses with thousands of agents waiting, only `half_open_max_calls` may probe; add a test that N concurrent callers at cooldown-expiry yield exactly `half_open_max_calls` probes, the rest routing to healthy endpoints or waiting — no stampede.
- (a) **Security:** fail-closed on all-unhealthy (never silently pick a dead endpoint); `Retry-After` respected prevents hammering (LLM10/SC-5). (b) **Scalability:** herd guard keeps recovery O(1) probes regardless of fleet size. (c) **Boundary:** all handling stays intra-pool; provider-level trips delegate up to CircuitBreaker/Fallback.

## Test Strategy

### Unit Tests (`test_load_balancer.py`) — ~24 tests
- Weighted RR distribution over many calls matches weights (±tolerance)
- Cursor advances monotonically and wraps
- `weight=0` excluded; all-zero → config error
- Health-aware skips a tripped endpoint; recovers after cooldown (HALF_OPEN probe)
- `_EndpointHealth` state transitions mirror circuit_breaker semantics
- All-unhealthy → `PoolExhaustedError`
- Sticky pins same key to same endpoint; eviction on unhealthy pin; missing key → RR
- Single-endpoint and empty-pool pass-through (FR-15)
- Per-endpoint key from env/vault, never a literal field (FR-18)
- OTel span attributes: chosen endpoint, strategy, healthy-count
- Shared registry: two module instances on one pool share a cursor (FR-6)
- Config validation: EndpointConfig HTTPS + weight + key-source

### Concurrency Test (`test_load_balancer_concurrency.py`) — ~3 tests
- **Interleaving-forced cursor test:** launch M concurrent `select_endpoint()` calls gated by an `asyncio.Barrier`/`Event` so they truly interleave; assert the multiset of issued slots equals the expected round-robin multiset — no slot issued twice, none skipped. (An instant mock would make `gather` run sequentially and pass even with an unsafe cursor — the barrier prevents that false pass.)
- Concurrent invokes with a failing endpoint: health recorded exactly once per failure, no lost updates.
- `clear_pools()` isolates state between tests.

### Integration Tests (in `test_registry.py` / `test_load_balancer.py`) — ~6 tests
- `load_model("openai", load_balance=True)` builds a pool from TOML
- Kwarg matrix: `True` / `dict` override / `False` / `None`
- Stack assembly: RateLimit/Retry/Queue wrap LB; LB innermost
- `MODULE_NAMES` contains `load_balance`; signature invariant holds
- No pool → identical single-adapter behavior (regression)
- Pool endpoint keys resolved via vault when `[vault].backend` set
