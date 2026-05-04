"""Tests for ``arcui.embedded_agents`` — the SPEC-023 bridge that
caches chat-loaded agents and registers them in the LIVE fleet.

The module wraps an executor's ``agent_factory`` with three concerns
(LRU cache, per-agent single-flight, fleet registration). These tests
hit each concern independently using fakes — no real ArcAgent needed.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from arcui.embedded_agents import (
    _BoundedAgentCache,
    install_embedded_agent_hooks,
)
from arcui.registry import AgentRegistry

pytestmark = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeExecutor:
    """Mimics arcgateway.executor.AsyncioExecutor's setter contract."""

    def __init__(self, factory: Any) -> None:
        self._agent_factory = factory

    @property
    def agent_factory(self) -> Any:
        return self._agent_factory

    def set_agent_factory(self, factory: Any) -> None:
        self._agent_factory = factory


class _FakeAgent:
    """Minimal stand-in exposing the duck-typed interface embedded_agents reads."""

    def __init__(self, name: str = "alpha", model: str = "anthropic/claude") -> None:
        self._config = types.SimpleNamespace(
            agent=types.SimpleNamespace(name=name),
            llm=types.SimpleNamespace(model=model, provider=model.split("/", 1)[0]),
        )


def _make_app(executor: _FakeExecutor) -> types.SimpleNamespace:
    """Build a minimal ``app`` whose ``app.state`` matches what arcui exposes."""
    state = types.SimpleNamespace(
        embedded_gateway=types.SimpleNamespace(executor=executor),
        agent_registry=AgentRegistry(),
    )
    return types.SimpleNamespace(state=state)


async def _noop_factory(agent_did: str) -> _FakeAgent:
    return _FakeAgent(name=agent_did.rsplit(":", 1)[-1])


# ── install_embedded_agent_hooks ──────────────────────────────────────────────


async def test_install_embedded_agent_hooks_idempotent() -> None:
    """Calling twice does not double-wrap the executor's factory."""
    executor = _FakeExecutor(_noop_factory)
    app = _make_app(executor)

    install_embedded_agent_hooks(app)
    first_wrap = executor.agent_factory
    install_embedded_agent_hooks(app)
    second_wrap = executor.agent_factory

    assert first_wrap is second_wrap, "second install_ must be a no-op"


async def test_install_no_op_when_no_embedded_gateway() -> None:
    """Apps without an embedded gateway are untouched."""
    app = types.SimpleNamespace(state=types.SimpleNamespace(embedded_gateway=None))
    install_embedded_agent_hooks(app)  # must not raise
    # No state attribute added.
    assert not hasattr(app.state, "embedded_agent_cache")


# ── factory cache + single-flight ─────────────────────────────────────────────


async def test_factory_caches_agent_per_did() -> None:
    """A second call for the same agent_did returns the cached instance."""
    load_count = {"n": 0}

    async def counting_factory(agent_did: str) -> _FakeAgent:
        load_count["n"] += 1
        return _FakeAgent(name=agent_did.rsplit(":", 1)[-1])

    executor = _FakeExecutor(counting_factory)
    app = _make_app(executor)
    install_embedded_agent_hooks(app)

    a1 = await executor.agent_factory("did:arc:agent:alpha")
    a2 = await executor.agent_factory("did:arc:agent:alpha")
    a3 = await executor.agent_factory("did:arc:agent:beta")

    assert a1 is a2, "second load must return cached instance"
    assert a1 is not a3, "different agent_dids must load independently"
    assert load_count["n"] == 2, "original factory called twice (once per unique DID)"


async def test_factory_concurrent_first_turn_singleflight() -> None:
    """Two concurrent first-turns for the same agent share one load."""
    load_started = asyncio.Event()
    load_release = asyncio.Event()
    load_count = {"n": 0}

    async def slow_factory(agent_did: str) -> _FakeAgent:
        load_count["n"] += 1
        load_started.set()
        await load_release.wait()
        return _FakeAgent(name="slow")

    executor = _FakeExecutor(slow_factory)
    app = _make_app(executor)
    install_embedded_agent_hooks(app)

    a_task = asyncio.create_task(executor.agent_factory("did:arc:agent:slow"))
    b_task = asyncio.create_task(executor.agent_factory("did:arc:agent:slow"))
    await load_started.wait()
    load_release.set()

    a, b = await asyncio.gather(a_task, b_task)
    assert a is b, "both concurrent calls must receive the same instance"
    assert load_count["n"] == 1, "single-flight: original factory called exactly once"


# ── fleet registration ────────────────────────────────────────────────────────


async def test_factory_registers_in_fleet() -> None:
    """The first load of an agent_did registers it in app.state.agent_registry."""
    executor = _FakeExecutor(_noop_factory)
    app = _make_app(executor)
    install_embedded_agent_hooks(app)
    assert app.state.agent_registry.list_agents() == []

    await executor.agent_factory("did:arc:agent:alpha")

    agents = app.state.agent_registry.list_agents()
    assert len(agents) == 1
    assert agents[0].agent_name == "alpha"
    assert agents[0].meta.get("source") == "embedded_chat"


async def test_factory_no_registry_no_crash() -> None:
    """When ``app.state.agent_registry`` is missing the factory still returns."""
    executor = _FakeExecutor(_noop_factory)
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            embedded_gateway=types.SimpleNamespace(executor=executor),
            # no agent_registry attribute at all
        )
    )
    install_embedded_agent_hooks(app)
    agent = await executor.agent_factory("did:arc:agent:alpha")
    assert agent is not None  # load succeeded; missing registry handled silently


# ── BoundedAgentCache LRU semantics ───────────────────────────────────────────


async def test_lru_cache_evicts_oldest_on_overflow() -> None:
    cache = _BoundedAgentCache(maxsize=2)
    cache.put("did:a", "agent-a")
    cache.put("did:b", "agent-b")
    cache.put("did:c", "agent-c")  # evicts did:a

    assert "did:a" not in cache
    assert cache.get("did:b") == "agent-b"
    assert cache.get("did:c") == "agent-c"


async def test_lru_cache_promotes_on_access() -> None:
    """get() moves the entry to most-recently-used so it survives the next eviction."""
    cache = _BoundedAgentCache(maxsize=2)
    cache.put("did:a", "agent-a")
    cache.put("did:b", "agent-b")
    cache.get("did:a")  # promote a
    cache.put("did:c", "agent-c")  # evicts b, not a

    assert cache.get("did:a") == "agent-a"
    assert "did:b" not in cache


async def test_lru_cache_evicts_lock_alongside_agent() -> None:
    """An evicted agent does not leave a stale lock behind."""
    cache = _BoundedAgentCache(maxsize=1)
    lock_a = cache.lock_for("did:a")
    cache.put("did:a", "agent-a")
    cache.put("did:b", "agent-b")  # evicts did:a + its lock

    new_lock_a = cache.lock_for("did:a")
    assert new_lock_a is not lock_a, "evicted agent gets a fresh lock on re-load"
