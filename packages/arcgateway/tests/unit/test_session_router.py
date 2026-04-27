"""Unit tests for SessionRouter and build_session_key.

Tests cover:
- Session key construction (determinism, cross-platform continuity, uniqueness)
- Basic handle() routing (new session spawns task, busy session queues)
- Queue depth tracking
- Active session counting
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    session_key: str = "abc123",
    message: str = "hello",
    user_did: str = "did:arc:user:alice",
    agent_did: str = "did:arc:agent:assistant",
    platform: str = "telegram",
) -> InboundEvent:
    """Build a minimal InboundEvent for testing."""
    return InboundEvent(
        platform=platform,
        chat_id="12345",
        user_did=user_did,
        agent_did=agent_did,
        session_key=session_key,
        message=message,
    )


class _ImmediateExecutor:
    """Executor that returns immediately with a single Done delta.

    Does NOT block, so tests can assert queue state synchronously.
    """

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


class _SlowExecutor:
    """Executor that waits for an external gate before completing.

    Used to hold a turn open so we can test queuing behaviour.

    Gate lifecycle: gates are pre-created in run() so that open_gate()
    can be called BEFORE the stream actually starts waiting. This avoids
    a test timing issue where open_gate() is called before the spawned
    task has had a chance to reach its first await.
    """

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}

    def open_gate(self, session_key: str) -> None:
        """Allow the turn for session_key to complete.

        Pre-creates the gate if it doesn't exist yet, so a gate opened
        before the stream starts will be seen immediately when the stream
        first checks.
        """
        gate = self.gates.setdefault(session_key, asyncio.Event())
        gate.set()

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Pre-create the gate so open_gate() works before the stream runs."""
        # Create gate eagerly so open_gate() called before the stream
        # starts will still be seen correctly.
        self.gates.setdefault(event.session_key, asyncio.Event())
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        gate = self.gates[event.session_key]
        await gate.wait()
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


# ---------------------------------------------------------------------------
# build_session_key tests
# ---------------------------------------------------------------------------


class TestBuildSessionKey:
    def test_deterministic(self) -> None:
        """Same inputs always produce the same key."""
        key1 = build_session_key("did:arc:agent:bot", "did:arc:user:alice")
        key2 = build_session_key("did:arc:agent:bot", "did:arc:user:alice")
        assert key1 == key2

    def test_different_users_produce_different_keys(self) -> None:
        """Different users get different sessions."""
        key_alice = build_session_key("did:arc:agent:bot", "did:arc:user:alice")
        key_bob = build_session_key("did:arc:agent:bot", "did:arc:user:bob")
        assert key_alice != key_bob

    def test_different_agents_produce_different_keys(self) -> None:
        """Same user with different agents gets different sessions."""
        key_a = build_session_key("did:arc:agent:assistant", "did:arc:user:alice")
        key_b = build_session_key("did:arc:agent:coder", "did:arc:user:alice")
        assert key_a != key_b

    def test_key_length_is_16_chars(self) -> None:
        """Keys are 16 hex characters (truncated SHA-256)."""
        key = build_session_key("agent", "user")
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    def test_cross_platform_same_user_same_key(self) -> None:
        """Same (agent, user) pair from different platforms gets same session.

        D-06: cross-platform session continuity depends on build_session_key
        taking agent_did + user_did only (NOT platform). The platform is
        resolved BEFORE session key construction via identity_graph.
        """
        key_telegram = build_session_key("did:arc:agent:bot", "did:arc:user:alice")
        key_slack = build_session_key("did:arc:agent:bot", "did:arc:user:alice")
        assert key_telegram == key_slack


# ---------------------------------------------------------------------------
# SessionRouter.handle() basic routing
# ---------------------------------------------------------------------------


class TestSessionRouterHandle:
    @pytest.mark.asyncio
    async def test_new_session_spawns_task(self) -> None:
        """handle() for a new session key spawns exactly one agent task."""
        router = SessionRouter(executor=_ImmediateExecutor())
        event = _make_event(session_key="key1")

        await router.handle(event)
        # Allow the spawned task to run
        await asyncio.sleep(0)

        assert router.agent_tasks_spawned.get("key1", 0) == 1

    @pytest.mark.asyncio
    async def test_second_message_to_busy_session_is_queued(self) -> None:
        """handle() queues a message if the session is already active."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        key = "busy_session"
        event1 = _make_event(session_key=key, message="first")
        event2 = _make_event(session_key=key, message="second")

        # Start first turn (will block at gate)
        await router.handle(event1)
        # Session is now active; second message should queue
        await router.handle(event2)

        # Exactly 1 task spawned so far
        assert router.agent_tasks_spawned.get(key, 0) == 1
        # Second message is in the queue
        assert len(router.queued_events.get(key, [])) == 1

        # Let the first turn complete
        slow_exec.open_gate(key)
        # Give the event loop a chance to drain the queue
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_active_session_count(self) -> None:
        """active_session_count() reflects currently running sessions."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        assert router.active_session_count() == 0

        await router.handle(_make_event(session_key="s1"))
        assert router.active_session_count() == 1

        await router.handle(_make_event(session_key="s2"))
        assert router.active_session_count() == 2

        # Yield to event loop so tasks reach their first await (gate.wait())
        # before we open the gates. Without this, open_gate would be called
        # before _stream has started waiting, and the gate set would be missed.
        await asyncio.sleep(0)

        # Let both complete
        slow_exec.open_gate("s1")
        slow_exec.open_gate("s2")
        # Give the event loop enough time to run both tasks to completion
        await asyncio.sleep(0.1)

        assert router.active_session_count() == 0

    @pytest.mark.asyncio
    async def test_queue_depth(self) -> None:
        """queue_depth() returns the correct count of pending events."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        key = "qtest"
        await router.handle(_make_event(session_key=key, message="m1"))
        await router.handle(_make_event(session_key=key, message="m2"))
        await router.handle(_make_event(session_key=key, message="m3"))

        assert router.queue_depth(key) == 2  # m1 running, m2+m3 queued

        slow_exec.open_gate(key)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_independent_sessions_run_concurrently(self) -> None:
        """Different session keys spawn independent tasks concurrently."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        await router.handle(_make_event(session_key="s_alice"))
        await router.handle(_make_event(session_key="s_bob"))

        assert router.active_session_count() == 2
        assert router.agent_tasks_spawned.get("s_alice", 0) == 1
        assert router.agent_tasks_spawned.get("s_bob", 0) == 1

        slow_exec.open_gate("s_alice")
        slow_exec.open_gate("s_bob")
        await asyncio.sleep(0.05)
