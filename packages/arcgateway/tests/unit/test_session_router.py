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
# Canonical session key — core owns key policy, adapters never hand-roll it
# ---------------------------------------------------------------------------


class _CapturingExecutor:
    """Records the event it was handed so tests can assert on it."""

    def __init__(self) -> None:
        self.events: list[InboundEvent] = []

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        self.events.append(event)
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


class TestCanonicalSessionKey:
    @pytest.mark.asyncio
    async def test_router_overrides_adapter_session_key_with_canonical(self) -> None:
        """A raw adapter-built key is replaced by the filename-safe canonical key.

        Regression: the Telegram adapter built
        ``"{agent_did}:telegram:private:{user}"``; with an agent DID like
        ``did:arc:local:executor/abc`` the '/' made it an unsafe filename and
        the executor's key validator raised ``ValueError`` on every message.
        """
        executor = _CapturingExecutor()
        router = SessionRouter(executor=executor)
        agent_did = "did:arc:local:executor/c659df43"
        event = _make_event(
            session_key=f"{agent_did}:telegram:private:8293394811",
            agent_did=agent_did,
            user_did="did:arc:telegram:8293394811",
        )

        await router.handle(event)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(executor.events) == 1
        key = executor.events[0].session_key
        # Filename-safe: no path separators, NUL, or dot-only.
        assert "/" not in key and "\\" not in key and ":" not in key
        assert key == build_session_key(agent_did, "did:arc:telegram:8293394811")


# ---------------------------------------------------------------------------
# Per-platform reply routing — reply returns to the originating platform
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """Outbound channel that records what it was asked to deliver."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[object, str]] = []
        self.placeholders: list[str] = []

    async def send(self, target: object, message: str, *, reply_to: str | None = None) -> None:
        self.sent.append((target, message))

    async def send_with_id(self, target: object, message: str) -> str | None:
        self.placeholders.append(message)
        return "mid-1"


class _EchoExecutor:
    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        yield Delta(kind="token", content="hi", is_final=False, turn_id=event.session_key)
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


class TestPerPlatformRouting:
    @pytest.mark.asyncio
    async def test_reply_routes_to_originating_platform(self) -> None:
        """A Telegram message's reply is delivered through the Telegram adapter only."""
        web = _RecordingAdapter("web")
        telegram = _RecordingAdapter("telegram")
        router = SessionRouter(executor=_EchoExecutor())
        router.register_adapter(web)
        router.register_adapter(telegram)

        await router.handle(_make_event(platform="telegram", user_did="did:arc:telegram:42"))
        await asyncio.sleep(0.05)

        assert telegram.sent, "telegram adapter should deliver the reply"
        assert not web.sent, "web adapter must NOT receive a telegram reply"

    @pytest.mark.asyncio
    async def test_single_adapter_fallback_delivers(self) -> None:
        """With one registered channel, any platform's reply goes through it."""
        only = _RecordingAdapter("web")
        router = SessionRouter(executor=_EchoExecutor())
        router.register_adapter(only)

        await router.handle(_make_event(platform="telegram"))
        await asyncio.sleep(0.05)

        assert only.sent, "single registered adapter should receive the reply"

    @pytest.mark.asyncio
    async def test_no_matching_adapter_does_not_misroute(self) -> None:
        """With several channels and no name match, nothing is misdelivered."""
        web = _RecordingAdapter("web")
        slack = _RecordingAdapter("slack")
        router = SessionRouter(executor=_EchoExecutor())
        router.register_adapter(web)
        router.register_adapter(slack)

        await router.handle(_make_event(platform="telegram"))
        await asyncio.sleep(0.05)

        assert not web.sent and not slack.sent, "unknown platform must not misroute"


# ---------------------------------------------------------------------------
# SessionRouter.handle() basic routing
# ---------------------------------------------------------------------------


class TestSessionRouterHandle:
    @pytest.mark.asyncio
    async def test_new_session_spawns_task(self) -> None:
        """handle() for a new session spawns exactly one agent task.

        The session is keyed by the canonical (agent, user) key the core
        derives — not the raw label an adapter happens to attach.
        """
        router = SessionRouter(executor=_ImmediateExecutor())
        event = _make_event()
        key = build_session_key(event.agent_did, event.user_did)

        await router.handle(event)
        await asyncio.sleep(0)

        assert router.agent_tasks_spawned.get(key, 0) == 1

    @pytest.mark.asyncio
    async def test_second_message_to_busy_session_is_queued(self) -> None:
        """A second message from the same (agent, user) queues behind the active turn."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        event1 = _make_event(message="first")
        event2 = _make_event(message="second")
        key = build_session_key(event1.agent_did, event1.user_did)

        await router.handle(event1)
        await router.handle(event2)

        assert router.agent_tasks_spawned.get(key, 0) == 1
        assert len(router.queued_events.get(key, [])) == 1

        slow_exec.open_gate(key)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_active_session_count(self) -> None:
        """active_session_count() reflects currently running sessions.

        Distinct users → distinct canonical keys → distinct sessions.
        """
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)
        agent = "did:arc:agent:assistant"
        alice = build_session_key(agent, "did:arc:user:alice")
        bob = build_session_key(agent, "did:arc:user:bob")

        assert router.active_session_count() == 0

        await router.handle(_make_event(user_did="did:arc:user:alice"))
        assert router.active_session_count() == 1

        await router.handle(_make_event(user_did="did:arc:user:bob"))
        assert router.active_session_count() == 2

        await asyncio.sleep(0)
        slow_exec.open_gate(alice)
        slow_exec.open_gate(bob)
        await asyncio.sleep(0.1)

        assert router.active_session_count() == 0

    @pytest.mark.asyncio
    async def test_queue_depth(self) -> None:
        """queue_depth() returns the correct count of pending events."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)

        event = _make_event(message="m1")
        key = build_session_key(event.agent_did, event.user_did)
        await router.handle(event)
        await router.handle(_make_event(message="m2"))
        await router.handle(_make_event(message="m3"))

        assert router.queue_depth(key) == 2  # m1 running, m2+m3 queued

        slow_exec.open_gate(key)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_independent_sessions_run_concurrently(self) -> None:
        """Different users spawn independent sessions concurrently."""
        slow_exec = _SlowExecutor()
        router = SessionRouter(executor=slow_exec)
        agent = "did:arc:agent:assistant"
        alice = build_session_key(agent, "did:arc:user:alice")
        bob = build_session_key(agent, "did:arc:user:bob")

        await router.handle(_make_event(user_did="did:arc:user:alice"))
        await router.handle(_make_event(user_did="did:arc:user:bob"))

        assert router.active_session_count() == 2
        assert router.agent_tasks_spawned.get(alice, 0) == 1
        assert router.agent_tasks_spawned.get(bob, 0) == 1

        slow_exec.open_gate(alice)
        slow_exec.open_gate(bob)
        await asyncio.sleep(0.05)
