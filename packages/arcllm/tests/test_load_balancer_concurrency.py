"""Concurrency tests for LoadBalancerModule (SPEC-017 T17.7).

Per the repo's "concurrency tests must force interleaving" standard: an
instant mock lets ``asyncio.gather`` run tasks effectively sequentially,
passing even with an unsafe cursor. These tests force genuine contention
with ``asyncio.Barrier`` (all tasks arrive at the critical section on the
same event-loop tick) rather than trusting gather() alone.

Design note on why an "unlock it and watch it break" test isn't literal
here: ``_select_weighted_rr``/``_select_health_aware``/``_select_sticky``
contain **no** ``await`` inside their `async with lock:` blocks (ADR-6 —
mutate-under-lock, await-outside-lock). Under asyncio's cooperative
single-threaded scheduler, a block with no internal ``await`` cannot be
preempted regardless of whether a lock guards it — there is no yield
point for another task to interleave on. That is the entire point of the
design (no serialization of `await inner.invoke()`), so it structurally
cannot be raced by removing the lock. To honestly prove the Barrier
technique would catch a real regression (e.g. a future refactor that
introduces an ``await`` inside an unlocked critical section), this file
includes a minimal reproduction of that anti-pattern
(``_naive_unlocked_increment``) run under the exact same Barrier harness,
demonstrating it DOES corrupt state — validating the test methodology.
The real production path is instead verified via an instrumented lock
that asserts the lock is actually acquired exactly once per call (catches
accidental removal of `async with self._pool_state.lock:`) and that the
resulting slot multiset is correct under forced concurrent contention.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMAPIError
from arcllm.modules.load_balancer import (
    LoadBalancerModule,
    PoolEndpoint,
    PoolExhaustedError,
    clear_pools,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)


def _make_adapter(name: str, latency: float = 0.0) -> MagicMock:
    adapter = MagicMock(spec=LLMProvider)
    adapter.name = name
    adapter.model_name = "m"
    adapter.validate_config.return_value = True
    adapter.close = AsyncMock()

    async def _invoke(*args: object, **kwargs: object) -> LLMResponse:
        if latency:
            await asyncio.sleep(latency)
        return _OK_RESPONSE

    adapter.invoke = AsyncMock(side_effect=_invoke)
    return adapter


def _make_pool(weights: list[int], latency: float = 0.0) -> list[PoolEndpoint]:
    return [
        PoolEndpoint(
            adapter=_make_adapter(f"ep{i}", latency=latency), weight=w, endpoint_id=f"ep{i}"
        )
        for i, w in enumerate(weights)
    ]


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


@pytest.fixture(autouse=True)
def _clean_pools():
    clear_pools()
    yield
    clear_pools()


class _InstrumentedLock(asyncio.Lock):
    """asyncio.Lock subclass that records acquire/release for assertion.

    Lets a test prove the production ``async with self._pool_state.lock:``
    statement is actually exercised (catches accidental removal) and that
    it is never re-entered while already held (mutual exclusion holds).
    """

    def __init__(self) -> None:
        super().__init__()
        self.acquire_count = 0
        self.max_concurrent_holders = 0
        self._current_holders = 0

    async def acquire(self) -> bool:
        result = await super().acquire()
        self.acquire_count += 1
        self._current_holders += 1
        self.max_concurrent_holders = max(self.max_concurrent_holders, self._current_holders)
        return result

    def release(self) -> None:
        self._current_holders -= 1
        super().release()


async def _barrier_gated_select(module: LoadBalancerModule, barrier: asyncio.Barrier) -> str:
    """Force all callers to reach the selection call on the same tick."""
    await barrier.wait()
    endpoint = await module._select_weighted_rr()
    return endpoint.endpoint_id


class TestBarrierForcedInterleaving:
    """FR-7 / SC-8: the shared cursor issues each slot exactly once."""

    async def test_no_double_issue_no_skip_under_forced_contention(self):
        n = 30
        pool = _make_pool([1, 1, 1])
        module = LoadBalancerModule({}, pool, "interleave-provider")

        instrumented = _InstrumentedLock()
        module._pool_state.lock = instrumented

        barrier = asyncio.Barrier(n)
        results = await asyncio.gather(*(_barrier_gated_select(module, barrier) for _ in range(n)))

        # Lock was genuinely exercised exactly once per call -- catches
        # accidental removal of `async with self._pool_state.lock:`.
        assert instrumented.acquire_count == n
        # Mutual exclusion held: never more than one holder at a time.
        assert instrumented.max_concurrent_holders == 1

        # The multiset of issued endpoint ids matches the expected
        # round-robin sequence exactly -- no double-issue, no skip.
        expected = [f"ep{i % 3}" for i in range(n)]
        assert sorted(results) == sorted(expected)
        # Cursor landed exactly where n selections from 0 would leave it.
        assert module._pool_state.cursor == n % 3

    async def test_barrier_methodology_catches_a_real_unlocked_race(self):
        """Proves the Barrier harness actually exposes races (not a false pass).

        Minimal reproduction of the anti-pattern this design avoids: a
        read-modify-write with an ``await`` between read and write, and
        no lock. Barrier-forced concurrent callers corrupt the shared
        counter -- validating that the harness used above would catch a
        real regression, not merely a well-behaved implementation.
        """
        n = 20
        state = {"cursor": 0}

        async def _naive_unlocked_increment(barrier: asyncio.Barrier) -> int:
            await barrier.wait()
            idx = state["cursor"]
            await asyncio.sleep(0)  # the anti-pattern: yields mid-critical-section
            state["cursor"] = idx + 1
            return idx

        barrier = asyncio.Barrier(n)
        results = await asyncio.gather(*(_naive_unlocked_increment(barrier) for _ in range(n)))

        # The race corrupts the sequence: duplicates and/or skips appear.
        assert sorted(results) != list(range(n)), (
            "expected the unlocked+await anti-pattern to corrupt the sequence "
            "under barrier-forced concurrency -- if this assertion fails, the "
            "harness is not actually forcing interleaving"
        )


class TestConcurrentHealthUpdates:
    """No lost updates when concurrent invokes fail against one endpoint."""

    async def test_concurrent_failures_recorded_exactly_once_each(self, messages):
        n = 25
        pool = _make_pool([1])
        pool[0].adapter.invoke = AsyncMock(side_effect=ArcLLMAPIError(500, "boom", "test"))
        module = LoadBalancerModule(
            {"strategy": "health_aware", "failure_threshold": 10_000},
            pool,
            "lost-update-provider",
        )

        barrier = asyncio.Barrier(n)

        async def _call() -> None:
            await barrier.wait()
            with pytest.raises(PoolExhaustedError):
                await module.invoke(messages)

        await asyncio.gather(*(_call() for _ in range(n)))

        health = module._pool_state.health["ep0"]
        assert health._consecutive_failures == n

    async def test_clear_pools_isolates_state_between_tests(self):
        pool = _make_pool([1, 1])
        module1 = LoadBalancerModule({}, pool, "isolation-provider")
        await module1._select_weighted_rr()
        assert module1._pool_state.cursor == 1

        clear_pools()

        pool2 = _make_pool([1, 1])
        module2 = LoadBalancerModule({}, pool2, "isolation-provider")
        assert module2._pool_state.cursor == 0


class TestContentionMicroBenchmark:
    """CLAUDE.md scalability: the shared cursor must not be a singleton bottleneck."""

    async def test_concurrent_invokes_complete_in_max_not_sum_latency(self, messages):
        latency = 0.05
        n = 20
        pool = _make_pool([1], latency=latency)
        module = LoadBalancerModule({}, pool, "benchmark-provider")

        start = time.monotonic()
        await asyncio.gather(*(module.invoke(messages) for _ in range(n)))
        elapsed = time.monotonic() - start

        # If the lock wrapped `await invoke()`, N calls would serialize to
        # ~N*latency. Correct (await-outside-lock) design completes in
        # ~latency regardless of N. Generous bound guards CI jitter.
        assert elapsed < latency * 3, (
            f"elapsed={elapsed:.3f}s suggests the pool lock is serializing "
            f"invokes (expected ~{latency:.3f}s for {n} concurrent calls)"
        )

    async def test_lock_hold_time_is_negligible_versus_invoke_latency(self, messages):
        """Lock hold (an int increment) stays orders of magnitude below invoke latency."""
        latency = 0.05
        pool = _make_pool([1, 1, 1], latency=latency)
        module = LoadBalancerModule({}, pool, "hold-time-provider")

        hold_times: list[float] = []
        real_lock = module._pool_state.lock

        class _TimedLock(type(real_lock)):
            async def __aenter__(self) -> "_TimedLock":
                self._t0 = time.perf_counter()  # type: ignore[attr-defined]
                await self.acquire()
                return self

            async def __aexit__(self, *exc: object) -> None:
                self.release()
                hold_times.append(time.perf_counter() - self._t0)  # type: ignore[attr-defined]

        timed = _TimedLock()
        module._pool_state.lock = timed

        for _ in range(10):
            await module.invoke(messages)

        assert hold_times, "lock was never acquired"
        # Hold time (sub-millisecond int math) must be a tiny fraction of
        # invoke latency -- proves the lock does not wrap the slow await.
        assert max(hold_times) < latency / 10


class TestRecoveryThunderingHerd:
    """Envoy-style herd guard: only half_open_max_calls probe a just-recovered endpoint.

    ep0's probe is given real latency so multiple in-flight probes can
    genuinely overlap (a zero-latency mock resolves before any sibling
    task even runs, trivially avoiding the herd risk this guard exists
    for). ep1 (already healthy) resolves instantly.
    """

    async def test_concurrent_callers_at_cooldown_expiry_yield_bounded_probes(
        self, messages, monkeypatch
    ):
        # NOTE: deliberately does NOT freeze time.monotonic here. asyncio's
        # own event loop uses time.monotonic() for real sleep/timeout
        # deadlines (loop.time() == time.monotonic()); ep0's probe below
        # uses a genuine `await asyncio.sleep(...)` to keep its probe
        # in-flight during the herd. Freezing monotonic globally would
        # freeze the event loop's own clock and hang every real sleep in
        # the process. Use a tiny real cooldown + a tiny real sleep to let
        # it elapse instead (see feedback_concurrency_tests_must_interleave).
        monkeypatch.setattr("arcllm.modules.load_balancer.random.uniform", lambda a, b: 0.0)

        half_open_max = 2
        m = 10
        pool = [
            PoolEndpoint(adapter=_make_adapter("ep0", latency=0.02), weight=1, endpoint_id="ep0"),
            PoolEndpoint(adapter=_make_adapter("ep1", latency=0.0), weight=1, endpoint_id="ep1"),
        ]
        module = LoadBalancerModule(
            {
                "strategy": "health_aware",
                "failure_threshold": 1,
                "cooldown_seconds": 0.01,
                "half_open_max_calls": half_open_max,
            },
            pool,
            "herd-provider",
        )
        # Trip ep0; ep1 stays healthy throughout.
        module._pool_state.health["ep0"].record_failure()
        await asyncio.sleep(0.02)  # let the real (tiny) cooldown elapse

        barrier = asyncio.Barrier(m)

        async def _call() -> None:
            await barrier.wait()
            await module.invoke(messages)

        await asyncio.gather(*(_call() for _ in range(m)))

        assert pool[0].adapter.invoke.await_count == half_open_max
        assert pool[1].adapter.invoke.await_count == m - half_open_max
