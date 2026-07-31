# PRD — Load Balancing (Intra-Provider Endpoint/Key Distribution)

## Problem Statement

ArcLLM resolves exactly one `base_url` and one API key per provider. Every `invoke()` for a given provider hits the same endpoint with the same key. In production with thousands of concurrent agents, this single-endpoint/single-key model breaks in three ways:

1. **One key throttles the whole fleet.** A provider rate-limits per API key. When 500 agents share one OpenAI/Anthropic key, the aggregate request rate slams the per-key quota and every agent gets 429s — even though the operator holds five keys that could carry the load. There is no way today to spread calls across those keys.

2. **One replica caps self-hosted throughput.** Teams run N vLLM/Ollama replicas serving the *same* model behind different `base_url`s for horizontal scale. ArcLLM can only point at one of them, so the other replicas sit idle while the targeted one saturates.

3. **Failover is not load spreading.** FallbackModule (spec 007) switches to a *different provider* only *after* the primary fails — it is inter-provider disaster recovery. It does nothing to distribute steady-state load across equivalent endpoints of the *same* provider, and it never engages until something is already broken. RoutingModule (spec 006) picks a provider by data *classification*, not by load. Neither spreads load across equivalent endpoints.

The gap: **no mechanism distributes a single caller's steady-state load across multiple equivalent endpoints/keys of one provider.**

## Goals

| # | Goal | Pillar | Success Metric |
|---|------|--------|----------------|
| G1 | Distribute invokes across a pool of same-provider endpoints/keys | Scalability | N endpoints share load per configured weights; aggregate throughput scales with pool size |
| G2 | Degrade gracefully when an endpoint is unhealthy | Scalability | Tripped endpoints skipped within one failure window; pool keeps serving on survivors |
| G3 | Preserve prompt-cache locality when requested | Scalability | Sticky mode pins a session/agent key to one endpoint; cache-hit rate preserved |
| G4 | Stay safe under thousands of concurrent agents | Scalability | Single shared cursor + health view per pool, no singleton bottleneck, no counter drift under interleaving |
| G5 | Add nothing when disabled; opt-in per call | Simplicity / Modularity | No imports, no latency, single-endpoint behavior byte-identical when `load_balance` absent |
| G6 | Resolve every endpoint key securely | Security | Each key via vault/env (no plaintext); health state carries no message content |
| G7 | Never overlap Fallback / Routing / cross-agent scheduling | Modularity | Boundaries documented and enforced; LB only selects an endpoint of the already-chosen provider |

## Success Criteria

- [ ] SC-1: A provider with an `[[endpoints]]` pool distributes invokes across all endpoints by weight (weighted round-robin)
- [ ] SC-2: Single-endpoint / no-pool providers behave exactly as today (degenerate pass-through)
- [ ] SC-3: `load_model("openai", load_balance=True)` enables balancing from config; `load_balance={...}` overrides strategy
- [ ] SC-4: A failing endpoint trips its per-endpoint circuit and is skipped by the health-aware strategy until cooldown
- [ ] SC-5: When all endpoints are unhealthy, invoke raises a clear pool-exhausted error (no silent hang)
- [ ] SC-6: Sticky mode routes the same session/agent key to the same endpoint across calls
- [ ] SC-7: The round-robin cursor and health state are shared per pool via an async-lock-guarded registry
- [ ] SC-8: A concurrency test that forces interleaving proves the shared cursor issues each pool slot exactly once (no double-issue, no skip)
- [ ] SC-9: Each endpoint's API key resolves via env var or vault path — never read from plaintext TOML
- [ ] SC-10: An endpoint with `weight = 0` is never selected (drained)
- [ ] SC-11: LB sits innermost; RateLimit, Retry, and Queue still wrap it and function unchanged
- [ ] SC-12: OTel spans emitted for endpoint selection (`arcllm.load_balance` with chosen endpoint + strategy)
- [ ] SC-13: All existing tests pass (no regressions)
- [ ] SC-14: >=90% coverage on new load-balancer code
- [ ] SC-15: `mypy --strict` and `ruff check` clean

## Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|-------------|----------|------------|
| FR-1 | Provider TOML accepts an `[[endpoints]]` array: each entry has `base_url`, `api_key_env` or `vault_path`, and `weight` | P0 | Config test: pool parses into typed `EndpointConfig` list |
| FR-2 | `EndpointConfig` inherits the same key-resolution + HTTPS validation as `ProviderSettings` | P0 | Unit test: HTTP remote endpoint rejected; localhost allowed |
| FR-3 | LoadBalancerModule builds one adapter per endpoint (own connection pool per endpoint) | P0 | Unit test: pool of 3 endpoints → 3 adapters with distinct base_urls |
| FR-4 | Weighted round-robin selects endpoints proportional to weight | P0 | Unit test: weights {2,1,1} over 400 calls → ~200/100/100 distribution |
| FR-5 | Round-robin cursor advances monotonically and wraps | P0 | Unit test: sequential selection order deterministic |
| FR-6 | Cursor + health live in a shared per-pool registry keyed by pool identity | P0 | Unit test: two module instances on same pool share one cursor |
| FR-7 | Registry access guarded by `asyncio.Lock`; safe under concurrent `invoke()` | P0 | Concurrency test forcing interleaving (barrier) proves no double-issue |
| FR-8 | Health-aware strategy skips endpoints whose per-endpoint circuit is OPEN | P0 | Unit test: failing endpoint tripped, then skipped |
| FR-9 | Per-endpoint circuit trips after `failure_threshold`, recovers after `cooldown_seconds` (HALF_OPEN probe) | P0 | Unit test mirroring circuit_breaker semantics per endpoint |
| FR-10 | On endpoint failure, LB records failure and (health-aware) tries the next healthy endpoint | P1 | Unit test: first endpoint errors → second serves |
| FR-11 | All endpoints unhealthy raises `PoolExhaustedError` (subclass of ArcLLM error) | P0 | Unit test: every endpoint tripped → clear raise, no hang |
| FR-12 | Sticky routing pins a caller (session/agent key from kwargs) to one endpoint | P1 | Unit test: same key → same endpoint across calls |
| FR-13 | Sticky key eviction falls back to weighted RR when the pinned endpoint is unhealthy | P1 | Unit test: pinned endpoint tripped → re-pinned to healthy endpoint |
| FR-14 | `weight = 0` endpoints are never selected (operator drain) | P1 | Unit test: zero-weight endpoint excluded |
| FR-15 | Single-endpoint pool (or empty pool) degenerates to direct pass-through | P0 | Unit test: no pool → behaves like today's single adapter |
| FR-16 | `load_model(..., load_balance=True|dict|False|None)` follows standard module resolution | P0 | Integration test: kwarg matrix |
| FR-17 | LoadBalancerModule occupies the innermost stack position, holding the pool (Router-like) | P0 | Integration test: stack assembled, RateLimit/Retry wrap the pool |
| FR-18 | Each endpoint key resolves via VaultResolver/env; never from plaintext TOML | P0 | Unit test: key comes from env/vault, not a literal string field |
| FR-19 | OTel span `arcllm.load_balance` records chosen endpoint + strategy + healthy-count | P1 | Unit test: span attributes present |
| FR-20 | `MODULE_NAMES` includes `load_balance`; kwarg matches signature invariant | P0 | `test_registry.py` invariant holds |

## Non-Functional Requirements

| ID | Requirement | Target | Measurement |
|----|-------------|--------|-------------|
| NFR-1 | Endpoint selection latency | <0.1ms per invoke | Benchmark (in-memory cursor + health read) |
| NFR-2 | Zero deps/imports when disabled | No load_balancer import when `load_balance` absent | Lazy-import verification |
| NFR-3 | Memory per pool | O(endpoints), not O(agents) | Shared registry, one cursor + health map per pool |
| NFR-4 | Concurrency safety | No double-issue / skip under interleaving | Barrier-forced concurrency test |
| NFR-5 | Connection reuse | One long-lived httpx pool per endpoint | Adapters built once, reused across invokes |
| NFR-6 | No singleton bottleneck | Lock held only around cursor/health mutation | Sleep/await happens outside the lock |

## User Stories

### US-1: Platform Operator with Multiple Keys
As an operator running 500 agents against OpenAI, I want to register five API keys as an endpoint pool so that aggregate request volume is spread across all five per-key quotas and my fleet stops hitting 429s.

### US-2: Self-Hosted Inference Owner
As the owner of four vLLM replicas serving one model, I want ArcLLM to round-robin across their four `base_url`s so that all replicas share the load instead of one saturating while three idle.

### US-3: Reliability Engineer
As an SRE, I want a replica that starts failing to be automatically skipped (and re-probed after cooldown) so that a single bad node degrades throughput gracefully instead of erroring a fraction of every agent's calls.

### US-4: Cost-Conscious Agent Developer
As a developer running long agent loops, I want sticky routing so that repeated calls from one agent hit the same endpoint and benefit from provider prompt-cache warmth, lowering cost and latency.

### US-5: Federal Deployment Engineer
As a federal ops engineer, I want every endpoint's key resolved from Vault (never plaintext in TOML) and load-distribution to reduce the chance any single endpoint is exhausted (NIST SC-5) so the deployment stays available under load.

## NIST / OWASP Mapping

| Control / Threat | How Load Balancing Addresses It |
|------------------|--------------------------------|
| **NIST 800-53 SC-5 (Denial of Service Protection)** | Distributing load across endpoints/keys prevents any single endpoint from being a throttling/exhaustion chokepoint. |
| **NIST 800-53 CP-family (Contingency / Availability)** | Complements failover (spec 007): steady-state load spreading + health-aware skipping keeps the service available on surviving endpoints without waiting for a hard failover. |
| **OWASP LLM10 (Unbounded Consumption)** | Spreading request volume across per-key quotas avoids per-key rate-limit exhaustion and the retry storms that follow; bounds consumption pressure on any one endpoint. |
| **Secure key handling (NIST IA/AU)** | Each endpoint key resolves via the same vault/env path (D-457) — no plaintext credentials in TOML. Health/cursor state carries counters and timestamps only, never message content. |
| **Scalability principle (CLAUDE.md)** | Shared-nothing per agent, shared-per-pool registry, no singleton bottleneck — supports 1,000s of concurrent agents. |

## Out of Scope

- **Cross-agent scheduling / fairness / global work-queue prioritization** — belongs to arcrun (loop/runtime), future spec. arcllm balances one caller's invoke across endpoints; it never arbitrates between agents. (D-455)
- Inter-provider failover (FallbackModule, spec 007) — different concern, different module.
- Classification-based provider selection (RoutingModule, spec 006) — different concern, different module.
- Latency-aware / least-connections adaptive strategies — future enhancement; v1 ships weighted RR + health-aware + optional sticky.
- Autoscaling or provisioning of endpoints (that is infrastructure, not arcllm).
- Distributed cursor across processes (v1 is per-process shared registry, mirroring rate_limit.py).

## Dependencies

| Dependency | Type | Notes |
|------------|------|-------|
| BaseModule (modules/base.py) | Internal | `_span()` for OTel, wrapper contract |
| registry.py | Internal | `_build_adapter()` per endpoint, `load_balance` kwarg, stack placement |
| config.py | Internal | `EndpointConfig` model, `[[endpoints]]` parsing, `[modules.load_balance]` |
| adapters/base.py | Internal | Per-endpoint `base_url` / key resolution via `ProviderConfig` |
| circuit_breaker.py | Internal | Per-endpoint health state machine (pattern reused, per endpoint) |
| rate_limit.py | Internal | Shared per-key registry + async-lock pattern (reused for per-pool cursor) |
| vault.py (spec 012) | Internal | Per-endpoint key resolution via VaultResolver |
| asyncio (stdlib) | External | `asyncio.Lock` guarding the shared registry |
