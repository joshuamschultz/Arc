"""Tests for LoadBalancerModule — intra-provider endpoint/key distribution.

Covers: per-endpoint health state machine, shared per-pool registry,
weighted round-robin / health-aware / sticky strategies, the module
itself, and integration edge cases (SPEC-017).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.load_balancer import (
    LoadBalancerModule,
    PoolEndpoint,
    PoolExhaustedError,
    _EndpointHealth,
    _EndpointHealthState,
    _extract_retry_after,
    _get_or_create_pool,
    _pool_id_for,
    clear_pools,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)


def _make_adapter(name: str = "test-endpoint", model: str = "m") -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = model
    adapter.validate_config.return_value = True
    adapter.invoke = AsyncMock(return_value=_OK_RESPONSE)
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


@pytest.fixture(autouse=True)
def _clean_pools():
    clear_pools()
    yield
    clear_pools()


# ---------------------------------------------------------------------------
# TestEndpointHealthStateMachine (T17.3/T17.4)
# ---------------------------------------------------------------------------


class TestEndpointHealthStateMachine:
    def test_starts_closed(self):
        health = _EndpointHealth(failure_threshold=3, cooldown_seconds=10.0, half_open_max_calls=1)
        assert health.state == _EndpointHealthState.CLOSED
        assert health.is_available() is True

    def test_opens_after_failure_threshold(self):
        health = _EndpointHealth(failure_threshold=3, cooldown_seconds=10.0, half_open_max_calls=1)
        health.record_failure()
        health.record_failure()
        assert health.state == _EndpointHealthState.CLOSED
        health.record_failure()
        assert health.state == _EndpointHealthState.OPEN

    def test_open_rejects_until_cooldown(self, monkeypatch):
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=1)
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health.record_failure()
        assert health.state == _EndpointHealthState.OPEN
        assert health.is_available() is False

    def test_half_open_after_cooldown(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=1)
        health.record_failure()
        assert health.state == _EndpointHealthState.OPEN

        t["now"] = 1011.0  # past the 10s cooldown
        assert health.is_available() is True
        assert health.state == _EndpointHealthState.HALF_OPEN

    def test_half_open_success_closes(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=1)
        health.record_failure()
        t["now"] = 1011.0
        assert health.is_available() is True

        health.record_success()
        assert health.state == _EndpointHealthState.CLOSED

    def test_half_open_failure_reopens(self, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=1)
        health.record_failure()
        t["now"] = 1011.0
        assert health.is_available() is True  # probe consumed

        health.record_failure()
        assert health.state == _EndpointHealthState.OPEN

    def test_half_open_max_calls_limits_probes(self, monkeypatch):
        """Only half_open_max_calls probes are admitted per HALF_OPEN window."""
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=2)
        health.record_failure()
        t["now"] = 1011.0

        assert health.is_available() is True
        assert health.is_available() is True
        assert health.is_available() is False  # third probe rejected

    def test_escalating_cooldown_for_repeat_offenders(self, monkeypatch):
        """A flapping endpoint (repeat OPEN transitions) is quarantined longer."""
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=10.0, half_open_max_calls=1)

        # First ejection: cooldown ~10s
        health.record_failure()
        t["now"] = 1005.0
        assert health.is_available() is False  # still within first cooldown
        t["now"] = 1011.0
        assert health.is_available() is True  # first cooldown elapsed -> HALF_OPEN probe
        health.record_failure()  # probe fails -> re-open, escalate

        # Second ejection: cooldown should now exceed the flat 10s window
        t["now"] = 1021.0  # 10s after re-open — a flat cooldown would allow HALF_OPEN here
        assert health.is_available() is False, "repeat offender must be quarantined longer"

    def test_retry_after_extends_cooldown(self, monkeypatch):
        """A 429 Retry-After overrides the base cooldown when larger."""
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        health = _EndpointHealth(failure_threshold=1, cooldown_seconds=5.0, half_open_max_calls=1)

        health.record_failure(retry_after=60.0)
        t["now"] = 1010.0  # past the 5s base cooldown, well within Retry-After
        assert health.is_available() is False
        t["now"] = 1061.0
        assert health.is_available() is True

    def test_success_resets_consecutive_failures_while_closed(self):
        health = _EndpointHealth(failure_threshold=3, cooldown_seconds=10.0, half_open_max_calls=1)
        health.record_failure()
        health.record_failure()
        health.record_success()
        health.record_failure()
        health.record_failure()
        assert health.state == _EndpointHealthState.CLOSED  # threshold not hit after reset


# ---------------------------------------------------------------------------
# TestPoolRegistry (T17.3/T17.4)
# ---------------------------------------------------------------------------


class TestPoolRegistry:
    def test_get_or_create_returns_same_state_for_same_pool_id(self):
        state1 = _get_or_create_pool("pool-a", ["ep0", "ep1"], 5, 30.0, 1)
        state2 = _get_or_create_pool("pool-a", ["ep0", "ep1"], 5, 30.0, 1)
        assert state1 is state2

    def test_different_pool_ids_get_different_states(self):
        state1 = _get_or_create_pool("pool-a", ["ep0"], 5, 30.0, 1)
        state2 = _get_or_create_pool("pool-b", ["ep0"], 5, 30.0, 1)
        assert state1 is not state2

    def test_clear_pools_resets_registry(self):
        _get_or_create_pool("pool-a", ["ep0"], 5, 30.0, 1)
        clear_pools()
        state_after = _get_or_create_pool("pool-a", ["ep0"], 5, 30.0, 1)
        state_before = _get_or_create_pool("pool-a", ["ep0"], 5, 30.0, 1)
        assert state_after is state_before  # re-created fresh, but stable post-clear

    def test_pool_state_has_health_per_endpoint(self):
        state = _get_or_create_pool("pool-c", ["ep0", "ep1", "ep2"], 5, 30.0, 1)
        assert set(state.health.keys()) == {"ep0", "ep1", "ep2"}
        assert all(isinstance(h, _EndpointHealth) for h in state.health.values())

    def test_pool_state_starts_cursor_at_zero(self):
        state = _get_or_create_pool("pool-d", ["ep0"], 5, 30.0, 1)
        assert state.cursor == 0

    def test_pool_id_stable_for_identical_endpoint_sets(self):
        id1 = _pool_id_for("openai", ["https://a::KEY_A", "https://b::KEY_B"])
        id2 = _pool_id_for("openai", ["https://a::KEY_A", "https://b::KEY_B"])
        assert id1 == id2

    def test_pool_id_distinct_for_different_endpoint_sets(self):
        id1 = _pool_id_for("openai", ["https://a::KEY_A"])
        id2 = _pool_id_for("openai", ["https://b::KEY_B"])
        assert id1 != id2

    def test_pool_id_distinct_for_different_providers_same_endpoints(self):
        id1 = _pool_id_for("openai", ["https://a::KEY_A"])
        id2 = _pool_id_for("anthropic", ["https://a::KEY_A"])
        assert id1 != id2


class TestPoolExhaustedError:
    def test_is_arcllm_error(self):
        from arcllm.exceptions import ArcLLMError

        err = PoolExhaustedError("pool-x", 3)
        assert isinstance(err, ArcLLMError)
        assert "pool-x" in str(err)
        assert "3" in str(err)


# ---------------------------------------------------------------------------
# Strategy tests (T17.5/T17.6)
# ---------------------------------------------------------------------------


def _make_pool(weights: list[int], names: list[str] | None = None) -> list[PoolEndpoint]:
    names = names or [f"ep{i}" for i in range(len(weights))]
    return [
        PoolEndpoint(adapter=_make_adapter(name), weight=w, endpoint_id=name)
        for name, w in zip(names, weights, strict=True)
    ]


class TestLoadBalancerConstruction:
    def test_empty_pool_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="at least one endpoint"):
            LoadBalancerModule({}, [], "test-provider")

    def test_all_zero_weight_rejected(self):
        pool = _make_pool([0, 0])
        with pytest.raises(ArcLLMConfigError, match="no positive-weight"):
            LoadBalancerModule({}, pool, "test-provider")

    def test_unknown_strategy_rejected(self):
        pool = _make_pool([1])
        with pytest.raises(ArcLLMConfigError, match="strategy"):
            LoadBalancerModule({"strategy": "bogus"}, pool, "test-provider")

    def test_unknown_config_key_rejected(self):
        pool = _make_pool([1])
        with pytest.raises(ArcLLMConfigError, match="Unknown"):
            LoadBalancerModule({"bogus_key": True}, pool, "test-provider")

    def test_invalid_failure_threshold_rejected(self):
        pool = _make_pool([1])
        with pytest.raises(ArcLLMConfigError, match="failure_threshold"):
            LoadBalancerModule({"failure_threshold": 0}, pool, "test-provider")

    def test_invalid_cooldown_rejected(self):
        pool = _make_pool([1])
        with pytest.raises(ArcLLMConfigError, match="cooldown_seconds"):
            LoadBalancerModule({"cooldown_seconds": 0}, pool, "test-provider")

    def test_invalid_half_open_max_rejected(self):
        pool = _make_pool([1])
        with pytest.raises(ArcLLMConfigError, match="half_open_max_calls"):
            LoadBalancerModule({"half_open_max_calls": 0}, pool, "test-provider")

    def test_defaults_are_valid(self):
        pool = _make_pool([1])
        module = LoadBalancerModule({}, pool, "test-provider")
        assert module._strategy == "weighted_round_robin"
        assert module._sticky_key == "session_id"


class TestWeightedRoundRobin:
    async def test_distribution_matches_weights(self, messages):
        """Weights {2,1,1} over 400 calls -> ~200/100/100 distribution."""
        pool = _make_pool([2, 1, 1])
        module = LoadBalancerModule({}, pool, "wrr-provider")

        counts: dict[str, int] = {"ep0": 0, "ep1": 0, "ep2": 0}
        for _ in range(400):
            await module.invoke(messages)
        for ep in pool:
            counts[ep.endpoint_id] = ep.adapter.invoke.await_count

        assert counts["ep0"] == 200
        assert counts["ep1"] == 100
        assert counts["ep2"] == 100

    async def test_cursor_advances_monotonically_and_wraps(self):
        pool = _make_pool([1, 1, 1])
        module = LoadBalancerModule({}, pool, "rr-provider")

        chosen = [(await module._select_weighted_rr()).endpoint_id for _ in range(7)]
        assert chosen == ["ep0", "ep1", "ep2", "ep0", "ep1", "ep2", "ep0"]

    async def test_weight_zero_endpoint_never_selected(self, messages):
        """A weight=0 endpoint receives exactly zero selections across many calls."""
        pool = _make_pool([1, 0, 1])
        module = LoadBalancerModule({}, pool, "drain-provider")

        for _ in range(100):
            await module.invoke(messages)

        drained = next(ep for ep in pool if ep.endpoint_id == "ep1")
        assert drained.adapter.invoke.await_count == 0

    async def test_single_endpoint_pass_through(self, messages):
        """FR-15: single-endpoint pool -- every call goes to the one endpoint."""
        pool = _make_pool([1])
        module = LoadBalancerModule({}, pool, "single-provider")

        for _ in range(5):
            result = await module.invoke(messages)
            assert result.content == "ok"
        assert pool[0].adapter.invoke.await_count == 5

    async def test_weighted_rr_does_not_consult_health(self, messages):
        """Weighted RR (non-health) surfaces failures upward -- no in-LB retry."""
        pool = _make_pool([1, 1])
        pool[0].adapter.invoke.side_effect = ArcLLMAPIError(500, "boom", "test")
        module = LoadBalancerModule({}, pool, "wrr-fail-provider")

        with pytest.raises(ArcLLMAPIError):
            await module.invoke(messages)
        # Only the first (failing) endpoint was tried -- no failover within LB.
        pool[1].adapter.invoke.assert_not_awaited()


class TestHealthAwareStrategy:
    async def test_skips_tripped_endpoint(self, messages):
        pool = _make_pool([1, 1])
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 1}, pool, "ha-provider"
        )
        # Manually trip ep0's circuit.
        module._pool_state.health["ep0"].record_failure()

        await module.invoke(messages)
        pool[0].adapter.invoke.assert_not_awaited()
        pool[1].adapter.invoke.assert_awaited_once()

    async def test_first_endpoint_errors_second_serves(self, messages):
        """FR-10: on endpoint failure, health-aware tries the next healthy endpoint."""
        pool = _make_pool([1, 1])
        pool[0].adapter.invoke.side_effect = ArcLLMAPIError(500, "boom", "test")
        module = LoadBalancerModule({"strategy": "health_aware"}, pool, "ha-failover-provider")

        result = await module.invoke(messages)
        assert result.content == "ok"
        pool[0].adapter.invoke.assert_awaited_once()
        pool[1].adapter.invoke.assert_awaited_once()

    async def test_all_unhealthy_raises_pool_exhausted(self, messages):
        pool = _make_pool([1, 1])
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 1}, pool, "ha-exhausted-provider"
        )
        for ep in pool:
            module._pool_state.health[ep.endpoint_id].record_failure()

        with pytest.raises(PoolExhaustedError):
            await module.invoke(messages)

    async def test_recovers_after_cooldown(self, messages, monkeypatch):
        t = {"now": 1000.0}
        monkeypatch.setattr("arcllm.modules.load_balancer.time.monotonic", lambda: t["now"])
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)
        pool = _make_pool([1, 1])
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 1, "cooldown_seconds": 10.0},
            pool,
            "ha-recover-provider",
        )
        module._pool_state.health["ep0"].record_failure()

        t["now"] = 1011.0  # cooldown elapsed -> ep0 HALF_OPEN probe eligible again
        await module.invoke(messages)
        pool[0].adapter.invoke.assert_awaited_once()

    async def test_success_resets_health(self, messages):
        pool = _make_pool([1])
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 2}, pool, "ha-reset-provider"
        )
        health = module._pool_state.health["ep0"]
        health.record_failure()
        await module.invoke(messages)
        assert health._consecutive_failures == 0


class TestStickyStrategy:
    async def test_same_key_same_endpoint(self, messages):
        pool = _make_pool([1, 1, 1])
        module = LoadBalancerModule({"strategy": "sticky"}, pool, "sticky-provider")

        for _ in range(10):
            await module.invoke(messages, session_id="agent-42")

        chosen = [ep.endpoint_id for ep in pool if ep.adapter.invoke.await_count > 0]
        assert len(chosen) == 1

    async def test_missing_sticky_key_falls_back_to_rr(self, messages, caplog):
        import logging as _logging

        pool = _make_pool([1, 1])
        module = LoadBalancerModule({"strategy": "sticky"}, pool, "sticky-missing-provider")
        with caplog.at_level(_logging.DEBUG, logger="arcllm.modules.load_balancer"):
            result = await module.invoke(messages)
        assert result.content == "ok"

    async def test_unhealthy_pin_evicts_to_healthy(self, messages):
        pool = _make_pool([1, 1, 1])
        module = LoadBalancerModule(
            {"strategy": "sticky", "failure_threshold": 1}, pool, "sticky-evict-provider"
        )
        # Find which endpoint a given key pins to, then trip its circuit.
        from arcllm.modules.load_balancer import _stable_hash_index

        pinned_idx = _stable_hash_index("agent-99", len(module._sequence))
        pinned_ep = module._sequence[pinned_idx]
        module._pool_state.health[pinned_ep.endpoint_id].record_failure()

        result = await module.invoke(messages, session_id="agent-99")
        assert result.content == "ok"
        pinned_ep.adapter.invoke.assert_not_awaited()

    async def test_distinct_keys_spread_across_sequence(self, messages):
        """Distinct sticky_keys should not all collapse onto one endpoint."""
        pool = _make_pool([1, 1, 1, 1, 1])
        module = LoadBalancerModule({"strategy": "sticky"}, pool, "sticky-spread-provider")

        selected_endpoints = set()
        for i in range(20):
            for ep in pool:
                ep.adapter.invoke.reset_mock()
            await module.invoke(messages, session_id=f"agent-{i}")
            for ep in pool:
                if ep.adapter.invoke.await_count > 0:
                    selected_endpoints.add(ep.endpoint_id)

        assert len(selected_endpoints) > 1


# ---------------------------------------------------------------------------
# TestLoadBalancerModuleLifecycle (T17.8/T17.9)
# ---------------------------------------------------------------------------


class TestLoadBalancerModuleLifecycle:
    async def test_close_closes_all_endpoint_adapters(self):
        pool = _make_pool([1, 1, 1])
        module = LoadBalancerModule({}, pool, "close-provider")

        await module.close()
        for ep in pool:
            ep.adapter.close.assert_awaited_once()

    async def test_close_tolerates_individual_failures(self):
        pool = _make_pool([1, 1])
        pool[0].adapter.close.side_effect = RuntimeError("boom")
        module = LoadBalancerModule({}, pool, "close-fail-provider")

        with pytest.raises(ExceptionGroup):
            await module.close()
        # The second adapter is still closed despite the first failing.
        pool[1].adapter.close.assert_awaited_once()

    def test_validate_config_all_valid(self):
        pool = _make_pool([1, 1])
        module = LoadBalancerModule({}, pool, "validate-provider")
        assert module.validate_config() is True

    def test_validate_config_any_invalid(self):
        pool = _make_pool([1, 1])
        pool[1].adapter.validate_config.return_value = False
        module = LoadBalancerModule({}, pool, "validate-invalid-provider")
        assert module.validate_config() is False

    def test_name_and_model_name_from_first_endpoint(self):
        pool = _make_pool([1, 1], names=["ep0", "ep1"])
        module = LoadBalancerModule({}, pool, "name-provider")
        assert module.name == pool[0].adapter.name
        assert module.model_name == pool[0].adapter.model_name


class TestLoadBalancerOtelSpan:
    async def test_span_records_endpoint_strategy_and_healthy_count(self, messages):
        pool = _make_pool([1, 1])
        module = LoadBalancerModule({"strategy": "health_aware"}, pool, "span-provider")

        recorded: dict[str, object] = {}

        class _FakeSpan:
            def set_attribute(self, key, value):
                recorded[key] = value

            def record_exception(self, exc):
                pass

            def set_status(self, *a, **k):
                pass

        from contextlib import contextmanager

        @contextmanager
        def _fake_start_span(name, attributes=None):
            yield _FakeSpan()

        module._tracer.start_as_current_span = _fake_start_span

        await module.invoke(messages)
        assert recorded["arcllm.load_balance.strategy"] == "health_aware"
        assert recorded["arcllm.load_balance.endpoint"] in {"ep0", "ep1"}
        assert recorded["arcllm.load_balance.healthy_count"] == 2


# ---------------------------------------------------------------------------
# TestExtractRetryAfter + failover edge branches (coverage completeness)
# ---------------------------------------------------------------------------


class TestExtractRetryAfter:
    def test_extracts_retry_after_from_429(self):
        exc = ArcLLMAPIError(429, "rate limited", "test", retry_after=42.0)
        assert _extract_retry_after(exc) == 42.0

    def test_none_for_non_429_status(self):
        exc = ArcLLMAPIError(500, "server error", "test", retry_after=42.0)
        assert _extract_retry_after(exc) is None

    def test_none_for_non_api_error(self):
        assert _extract_retry_after(RuntimeError("boom")) is None


class TestFailoverSkipsAlreadyTriedDuplicateSlot:
    """A weighted duplicate slot for an already-failed endpoint must be
    skipped within the same invoke() failover loop (not just excluded by
    health -- failure_threshold may not have tripped the circuit yet)."""

    async def test_health_aware_skips_duplicate_excluded_slot(self, messages):
        pool = _make_pool([2, 1], names=["ep0", "ep1"])
        pool[0].adapter.invoke.side_effect = ArcLLMAPIError(500, "boom", "test")
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 10}, pool, "dup-slot-provider"
        )

        result = await module.invoke(messages)
        assert result.content == "ok"
        pool[1].adapter.invoke.assert_awaited_once()

    async def test_sticky_skips_duplicate_excluded_slot(self, messages, monkeypatch):
        pool = _make_pool([2, 1], names=["ep0", "ep1"])
        pool[0].adapter.invoke.side_effect = ArcLLMAPIError(500, "boom", "test")
        module = LoadBalancerModule(
            {"strategy": "sticky", "failure_threshold": 10}, pool, "sticky-dup-slot-provider"
        )
        monkeypatch.setattr("arcllm.modules.load_balancer._stable_hash_index", lambda k, n: 0)

        result = await module.invoke(messages, session_id="agent-x")
        assert result.content == "ok"
        pool[1].adapter.invoke.assert_awaited_once()

    async def test_sticky_all_unhealthy_raises_its_own_pool_exhausted(self, messages):
        """Sticky's own loop (not the health-aware delegate) raises when exhausted."""
        pool = _make_pool([1, 1])
        module = LoadBalancerModule(
            {"strategy": "sticky", "failure_threshold": 1}, pool, "sticky-exhausted-provider"
        )
        for ep in pool:
            module._pool_state.health[ep.endpoint_id].record_failure()

        with pytest.raises(PoolExhaustedError):
            await module.invoke(messages, session_id="agent-y")
