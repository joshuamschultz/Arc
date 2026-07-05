"""LoadBalancerModule — intra-provider endpoint/key distribution (SPEC-017).

Distributes a single caller's ``invoke()`` calls across a pool of
*equivalent endpoints of the same provider* — N self-hosted replicas
serving one model, or N API keys for one provider raising the aggregate
rate limit. Strictly **intra-provider** (ADR-1): it never changes which
provider or model answers. That is FallbackModule's job (provider-on-
failure) and RoutingModule's job (provider-by-classification).

Implements NIST 800-53 SC-5(2) (Capacity, Bandwidth, and Redundancy):
distributing load across endpoints/keys prevents any single endpoint from
being a denial-of-service chokepoint.

Three strategies (ADR-2/3/4):
    - ``weighted_round_robin`` (default): shared cursor over a weighted
      selection sequence. No health awareness — failures surface to
      Retry/Fallback above unchanged.
    - ``health_aware``: skips endpoints whose per-endpoint circuit is
      OPEN; on invoke failure, records the failure and tries the next
      healthy endpoint (bounded to pool size) before raising
      ``PoolExhaustedError``.
    - ``sticky``: pins a caller key (kwarg named by ``sticky_key``) to one
      endpoint via a stable hash for prompt-cache locality; evicts to
      health-aware selection when the pinned endpoint is unhealthy.

The round-robin cursor and per-endpoint health live in a **shared
per-pool registry** guarded by ``asyncio.Lock`` (ADR-6), mirroring
``rate_limit.py``'s ``_bucket_registry``: one cursor and one health view
per pool regardless of agent count — no singleton bottleneck. The lock
guards only the cursor increment / health dict mutation; ``await
inner.invoke()`` always runs outside the lock so concurrent callers are
never serialized.

``_EndpointHealth`` is a per-endpoint circuit breaker reusing
``circuit_breaker.py``'s CLOSED/OPEN/HALF_OPEN semantics, but passive
(Envoy outlier-detection pattern): HALF_OPEN reuses the next real invoke
as its probe rather than issuing a synthetic health-check call. Cooldown
escalates for repeat offenders and carries +/-10-20% jitter so pooled
agents don't all re-probe a just-recovered endpoint on the same tick
(recovery thundering-herd guard).
"""

import asyncio
import hashlib
import logging
import random
import time
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError, ArcLLMError
from arcllm.modules.base import validate_config_keys
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_CONFIG_KEYS = {
    "strategy",
    "sticky_key",
    "failure_threshold",
    "cooldown_seconds",
    "half_open_max_calls",
    "enabled",
}

_VALID_STRATEGIES = frozenset({"weighted_round_robin", "health_aware", "sticky"})

# Escalating cooldown caps at 2**_MAX_ESCALATION_STEPS * cooldown_seconds so a
# permanently-flapping endpoint doesn't grow its quarantine unboundedly.
_MAX_ESCALATION_STEPS = 4

# Recovery-jitter band applied to the effective cooldown (Envoy outlier
# detection pattern) — prevents every waiting agent from re-probing a
# just-recovered endpoint on the same tick.
_JITTER_LOW = -0.2
_JITTER_HIGH = 0.2


class PoolExhaustedError(ArcLLMError):
    """Raised when every endpoint in a pool is unhealthy (FR-11).

    Fail-closed: LoadBalancerModule never hangs and never silently
    selects a dead endpoint. Callers may retry later, alert, or (with a
    wrapping FallbackModule) fail over to a different provider entirely.
    """

    def __init__(self, pool_id: str, endpoint_count: int) -> None:
        self.pool_id = pool_id
        self.endpoint_count = endpoint_count
        super().__init__(f"All {endpoint_count} endpoints in pool '{pool_id}' are unhealthy")


class _EndpointHealthState(StrEnum):
    """Per-endpoint circuit states — mirrors circuit_breaker.py."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _EndpointHealth:
    """Per-endpoint circuit breaker owned by the pool (finer-grained than
    the per-provider ``CircuitBreakerModule``, per ADR-3).

    Passive health only (ADR-3 Research Insights): no synthetic probe
    calls are ever issued. The next real invoke *is* the HALF_OPEN probe.
    Cooldown escalates with consecutive ejections and carries jitter,
    computed once at the moment of ejection (not re-rolled on every
    ``is_available()`` check, so the deadline is stable and testable).
    """

    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: float,
        half_open_max_calls: int,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._half_open_max = half_open_max_calls

        self._state = _EndpointHealthState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_ejections = 0
        self._half_open_calls = 0
        self._cooldown_deadline: float | None = None

    @property
    def state(self) -> _EndpointHealthState:
        return self._state

    def is_available(self) -> bool:
        """Return True if this endpoint may be selected right now.

        Caller must hold the pool lock (mutates state). Transitions
        OPEN -> HALF_OPEN once the jittered/escalated cooldown deadline
        has passed, then admits at most ``half_open_max_calls`` probes.
        """
        if self._state == _EndpointHealthState.CLOSED:
            return True

        if self._state == _EndpointHealthState.OPEN:
            if self._cooldown_deadline is None or time.monotonic() >= self._cooldown_deadline:
                self._state = _EndpointHealthState.HALF_OPEN
                self._half_open_calls = 0
            else:
                return False

        # HALF_OPEN: admit at most half_open_max_calls probes (herd guard).
        if self._half_open_calls >= self._half_open_max:
            return False
        self._half_open_calls += 1
        return True

    def record_success(self) -> None:
        """Record a successful invoke against this endpoint."""
        if self._state == _EndpointHealthState.HALF_OPEN:
            self._state = _EndpointHealthState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_ejections = 0
            self._half_open_calls = 0
            self._cooldown_deadline = None
        elif self._state == _EndpointHealthState.CLOSED:
            self._consecutive_failures = 0

    def record_failure(self, retry_after: float | None = None) -> None:
        """Record a failed invoke; may trip (or re-trip) the circuit.

        Args:
            retry_after: A provider-supplied Retry-After (seconds) from a
                429 response. When present, the effective cooldown is
                ``max(escalated_cooldown, retry_after)`` (Cloudflare/Portkey
                gateway pattern) — a rate-limited key isn't re-probed
                before the provider says it's ready.
        """
        self._consecutive_failures += 1

        if self._state == _EndpointHealthState.HALF_OPEN:
            self._half_open_calls = 0
            self._trip(retry_after)
        elif self._state == _EndpointHealthState.CLOSED:
            if self._consecutive_failures >= self._failure_threshold:
                self._trip(retry_after)

    def _trip(self, retry_after: float | None) -> None:
        """Transition to OPEN, computing the jittered escalating cooldown once."""
        self._consecutive_ejections += 1
        steps = min(self._consecutive_ejections - 1, _MAX_ESCALATION_STEPS)
        effective = self._cooldown_seconds * (2**steps)
        if retry_after is not None:
            effective = max(effective, retry_after)
        # Timing jitter for thundering-herd avoidance, not a cryptographic
        # use of randomness (bandit S311 false positive).
        jitter = effective * random.uniform(_JITTER_LOW, _JITTER_HIGH)  # noqa: S311
        self._cooldown_deadline = time.monotonic() + effective + jitter
        self._state = _EndpointHealthState.OPEN


# ---------------------------------------------------------------------------
# Shared per-pool registry (mirrors rate_limit.py's _bucket_registry)
# ---------------------------------------------------------------------------


class _PoolState:
    """Shared state for one endpoint pool: RR cursor + per-endpoint health.

    One instance per distinct pool_id regardless of how many
    LoadBalancerModule instances or agents share it (ADR-6, FR-6). The
    ``asyncio.Lock`` guards cursor/health mutation only — ``await
    inner.invoke()`` always happens outside it (D-454), so callers are
    never serialized on the shared pool.
    """

    __slots__ = ("cursor", "health", "lock")

    def __init__(
        self,
        endpoint_ids: list[str],
        failure_threshold: int,
        cooldown_seconds: float,
        half_open_max_calls: int,
    ) -> None:
        self.cursor = 0
        self.health: dict[str, _EndpointHealth] = {
            ep_id: _EndpointHealth(failure_threshold, cooldown_seconds, half_open_max_calls)
            for ep_id in endpoint_ids
        }
        self.lock = asyncio.Lock()


_pool_registry: dict[str, _PoolState] = {}


def _pool_id_for(provider_name: str, endpoint_ids: list[str]) -> str:
    """Stable identity hash for a pool: provider name + endpoint identities.

    Two ``LoadBalancerModule`` instances constructed over the same
    provider + endpoint set resolve to the same pool_id and therefore
    share one ``_PoolState`` (FR-6).
    """
    raw = provider_name + "|" + "|".join(endpoint_ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _get_or_create_pool(
    pool_id: str,
    endpoint_ids: list[str],
    failure_threshold: int,
    cooldown_seconds: float,
    half_open_max_calls: int,
) -> _PoolState:
    """Return the shared ``_PoolState`` for *pool_id*, creating one if needed."""
    if pool_id not in _pool_registry:
        _pool_registry[pool_id] = _PoolState(
            endpoint_ids, failure_threshold, cooldown_seconds, half_open_max_calls
        )
    return _pool_registry[pool_id]


def clear_pools() -> None:
    """Remove all shared pool state (test isolation and registry.clear_cache())."""
    _pool_registry.clear()


# ---------------------------------------------------------------------------
# LoadBalancerModule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolEndpoint:
    """One endpoint in a load-balanced pool: its adapter, weight, and stable id.

    ``endpoint_id`` is a stable identity string (base_url + key-source)
    computed by the caller (registry.py) from ``EndpointConfig`` — this
    module is deliberately agnostic of config internals.
    """

    adapter: LLMProvider
    weight: int
    endpoint_id: str


def _stable_hash_index(key: str, modulo: int) -> int:
    """Stable hash of *key* into ``[0, modulo)`` for sticky pinning.

    NFKC-normalizes first (ADR-4 Research Insights): a caller-supplied
    session/agent id may carry user-adjacent Unicode, and normalizing
    prevents two visually-identical keys from hashing to different slots.
    """
    normalized = unicodedata.normalize("NFKC", key)
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") % modulo


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract a 429 Retry-After (seconds) from *exc*, else None."""
    if isinstance(exc, ArcLLMAPIError) and exc.status_code == 429:
        return exc.retry_after
    return None


class LoadBalancerModule(LLMProvider):
    """Distributes invoke() calls across a pool of same-provider endpoints.

    Holds a pool of pre-built endpoint adapters and occupies the
    **innermost** stack position (ADR-7), replacing the single adapter —
    exactly like ``RoutingModule``. RateLimit, Retry, Fallback, and
    CircuitBreaker still wrap it unchanged: this module distributes
    *within* the aggregate cap those modules enforce, it never lifts it
    (LLM10 boundary, see SDD Research Insights).

    Config keys:
        strategy: "weighted_round_robin" (default) | "health_aware" | "sticky".
        sticky_key: kwarg name read for the sticky pin (default: "session_id").
        failure_threshold: consecutive failures before an endpoint's
            circuit opens (default: 5).
        cooldown_seconds: base cooldown before a HALF_OPEN probe is
            admitted (default: 30.0); escalates and jitters per endpoint.
        half_open_max_calls: max concurrent HALF_OPEN probes per endpoint
            (default: 1).
    """

    def __init__(
        self,
        config: dict[str, Any],
        pool: list[PoolEndpoint],
        provider_name: str,
    ) -> None:
        validate_config_keys(config, _VALID_CONFIG_KEYS, "LoadBalancerModule")

        if not pool:
            raise ArcLLMConfigError(
                "LoadBalancerModule requires at least one endpoint with weight > 0"
            )

        self._strategy: str = config.get("strategy", "weighted_round_robin")
        if self._strategy not in _VALID_STRATEGIES:
            raise ArcLLMConfigError(
                f"strategy must be one of {sorted(_VALID_STRATEGIES)}, got {self._strategy!r}"
            )
        self._sticky_key: str = config.get("sticky_key", "session_id")

        failure_threshold: int = config.get("failure_threshold", 5)
        cooldown_seconds: float = config.get("cooldown_seconds", 30.0)
        half_open_max_calls: int = config.get("half_open_max_calls", 1)
        if failure_threshold < 1:
            raise ArcLLMConfigError("failure_threshold must be >= 1")
        if cooldown_seconds <= 0:
            raise ArcLLMConfigError("cooldown_seconds must be > 0")
        if half_open_max_calls < 1:
            raise ArcLLMConfigError("half_open_max_calls must be >= 1")

        self._pool: list[PoolEndpoint] = list(pool)  # defensive copy
        self._provider_name = provider_name
        self._tracer = trace.get_tracer("arcllm")

        # Expand into a weighted selection sequence: weight=2 appears twice.
        # Deterministic RR beats weighted-random at low volume (ADR-2
        # Research Insights) — exact proportions even over few calls.
        self._sequence: list[PoolEndpoint] = [ep for ep in self._pool for _ in range(ep.weight)]
        if not self._sequence:
            raise ArcLLMConfigError("LoadBalancerModule pool has no positive-weight endpoints")

        endpoint_ids = [ep.endpoint_id for ep in self._pool]
        self._pool_id = _pool_id_for(provider_name, endpoint_ids)
        self._pool_state = _get_or_create_pool(
            self._pool_id, endpoint_ids, failure_threshold, cooldown_seconds, half_open_max_calls
        )

    @property
    def name(self) -> str:
        return self._pool[0].adapter.name

    @property
    def model_name(self) -> str:
        return self._pool[0].adapter.model_name

    # -- Selection strategies -------------------------------------------------
    #
    # Mutate-under-lock / await-outside-lock discipline throughout (ADR-6):
    # the pool lock guards only the cursor increment and health-dict reads/
    # mutations, never an `await inner.invoke()`.

    async def _select_weighted_rr(self) -> PoolEndpoint:
        """Advance the shared cursor and return the endpoint at that slot."""
        async with self._pool_state.lock:
            idx = self._pool_state.cursor
            self._pool_state.cursor = (idx + 1) % len(self._sequence)
        return self._sequence[idx]

    async def _select_health_aware(self, exclude: frozenset[str] = frozenset()) -> PoolEndpoint:
        """Skip endpoints whose circuit is OPEN or already tried this call.

        Re-reads health state inside the lock at decision time (no stale
        snapshot) — guards against lost updates under concurrent failures.
        """
        async with self._pool_state.lock:
            n = len(self._sequence)
            start = self._pool_state.cursor
            for offset in range(n):
                idx = (start + offset) % n
                candidate = self._sequence[idx]
                if candidate.endpoint_id in exclude:
                    continue
                if self._pool_state.health[candidate.endpoint_id].is_available():
                    self._pool_state.cursor = (idx + 1) % n
                    return candidate
            raise PoolExhaustedError(self._pool_id, len(self._pool))

    async def _select_sticky(
        self, sticky_value: str | None, exclude: frozenset[str] = frozenset()
    ) -> PoolEndpoint:
        """Pin *sticky_value* to a stable endpoint; evict to health-aware on unhealthy pin."""
        if sticky_value is None:
            logger.debug(
                "Sticky key '%s' not present in kwargs; falling back to weighted RR",
                self._sticky_key,
            )
            return await self._select_health_aware(exclude=exclude)

        n = len(self._sequence)
        pinned_idx = _stable_hash_index(sticky_value, n)

        async with self._pool_state.lock:
            for offset in range(n):
                idx = (pinned_idx + offset) % n
                candidate = self._sequence[idx]
                if candidate.endpoint_id in exclude:
                    continue
                if self._pool_state.health[candidate.endpoint_id].is_available():
                    return candidate
            raise PoolExhaustedError(self._pool_id, len(self._pool))

    def _count_healthy(self) -> int:
        """Advisory count of non-OPEN endpoints for OTel telemetry (FR-19).

        Read without the lock — a point-in-time snapshot for observability,
        never used as a selection decision.
        """
        return sum(
            1 for h in self._pool_state.health.values() if h.state != _EndpointHealthState.OPEN
        )

    async def _invoke_endpoint(
        self,
        endpoint: PoolEndpoint,
        messages: list[Message],
        tools: list[Tool] | None,
        record_health: bool,
        **kwargs: Any,
    ) -> LLMResponse:
        """Invoke the chosen endpoint under an OTel span; optionally record health."""
        with self._tracer.start_as_current_span("arcllm.load_balance") as span:
            span.set_attribute("arcllm.load_balance.endpoint", endpoint.endpoint_id)
            span.set_attribute("arcllm.load_balance.strategy", self._strategy)
            span.set_attribute("arcllm.load_balance.healthy_count", self._count_healthy())
            try:
                response = await endpoint.adapter.invoke(messages, tools, **kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            if record_health:
                async with self._pool_state.lock:
                    self._pool_state.health[endpoint.endpoint_id].record_success()
            return response

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if self._strategy == "weighted_round_robin":
            endpoint = await self._select_weighted_rr()
            return await self._invoke_endpoint(
                endpoint, messages, tools, record_health=False, **kwargs
            )

        sticky_value: str | None = None
        if self._strategy == "sticky":
            sticky_value = kwargs.get(self._sticky_key)

        tried: set[str] = set()
        last_exc: Exception | None = None
        for _ in range(len(self._pool)):
            if self._strategy == "sticky":
                endpoint = await self._select_sticky(sticky_value, exclude=frozenset(tried))
            else:
                endpoint = await self._select_health_aware(exclude=frozenset(tried))
            tried.add(endpoint.endpoint_id)
            try:
                return await self._invoke_endpoint(
                    endpoint, messages, tools, record_health=True, **kwargs
                )
            except Exception as exc:  # reason: record failure, try next healthy endpoint (FR-10)
                last_exc = exc
                retry_after = _extract_retry_after(exc)
                async with self._pool_state.lock:
                    self._pool_state.health[endpoint.endpoint_id].record_failure(retry_after)
        raise PoolExhaustedError(self._pool_id, len(self._pool)) from last_exc

    def validate_config(self) -> bool:
        """All pool adapters must be valid."""
        return all(ep.adapter.validate_config() for ep in self._pool)

    async def close(self) -> None:
        """Close every endpoint adapter, tolerating individual failures."""
        errors: list[Exception] = []
        for ep in self._pool:
            try:
                await ep.adapter.close()
            except Exception as exc:  # reason: fail-open — log + continue closing the rest
                logger.error("Failed to close endpoint adapter '%s': %s", ep.endpoint_id, exc)
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close some pool adapters", errors)
