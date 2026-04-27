"""Unit tests for QueueManager — bounded FIFO with idle TTL eviction."""

from __future__ import annotations

import time

from arcgateway.executor import InboundEvent
from arcgateway.session_queue import QueueManager


def _make_event(session_key: str = "s1", message: str = "hi") -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="chat1",
        user_did="did:arc:user:alice",
        agent_did="did:arc:agent:bot",
        session_key=session_key,
        message=message,
    )


# ---------------------------------------------------------------------------
# Basic enqueue / dequeue
# ---------------------------------------------------------------------------


class TestQueueManagerBasic:
    def test_enqueue_returns_true_on_success(self) -> None:
        mgr = QueueManager()
        event = _make_event()
        result = mgr.enqueue("s1", event)
        assert result is True

    def test_depth_after_enqueue(self) -> None:
        mgr = QueueManager()
        assert mgr.depth("s1") == 0
        mgr.enqueue("s1", _make_event(message="m1"))
        mgr.enqueue("s1", _make_event(message="m2"))
        assert mgr.depth("s1") == 2

    def test_dequeue_returns_events_in_fifo_order(self) -> None:
        mgr = QueueManager()
        e1 = _make_event(message="first")
        e2 = _make_event(message="second")
        mgr.enqueue("s1", e1)
        mgr.enqueue("s1", e2)

        assert mgr.dequeue("s1") is e1
        assert mgr.dequeue("s1") is e2
        assert mgr.dequeue("s1") is None

    def test_dequeue_on_unknown_session_returns_none(self) -> None:
        mgr = QueueManager()
        assert mgr.dequeue("nonexistent") is None

    def test_depth_on_unknown_session_returns_zero(self) -> None:
        mgr = QueueManager()
        assert mgr.depth("no_such_key") == 0

    def test_snapshot_reflects_current_queue(self) -> None:
        mgr = QueueManager()
        e1 = _make_event(message="a")
        e2 = _make_event(message="b")
        mgr.enqueue("s1", e1)
        mgr.enqueue("s1", e2)
        snap = mgr.snapshot("s1")
        assert len(snap) == 2
        assert snap[0] is e1
        assert snap[1] is e2

    def test_snapshot_returns_empty_list_for_unknown(self) -> None:
        mgr = QueueManager()
        assert mgr.snapshot("ghost") == []


# ---------------------------------------------------------------------------
# Bounded depth
# ---------------------------------------------------------------------------


class TestQueueManagerBoundedDepth:
    def test_enqueue_drops_event_when_at_max_depth(self) -> None:
        """Events beyond max_depth are dropped and enqueue returns False."""
        mgr = QueueManager(max_depth=3)
        for i in range(3):
            result = mgr.enqueue("s1", _make_event(message=f"m{i}"))
            assert result is True

        # 4th event should be dropped
        dropped = mgr.enqueue("s1", _make_event(message="overflow"))
        assert dropped is False
        assert mgr.depth("s1") == 3

    def test_enqueue_accepts_again_after_dequeue(self) -> None:
        """After dequeue frees space, new events can be enqueued."""
        mgr = QueueManager(max_depth=2)
        mgr.enqueue("s1", _make_event(message="a"))
        mgr.enqueue("s1", _make_event(message="b"))
        assert mgr.enqueue("s1", _make_event(message="c")) is False

        mgr.dequeue("s1")
        assert mgr.enqueue("s1", _make_event(message="c")) is True

    def test_independent_sessions_have_separate_bounds(self) -> None:
        """Each session has its own queue; one session being full does not affect others."""
        mgr = QueueManager(max_depth=2)
        mgr.enqueue("s1", _make_event(message="a"))
        mgr.enqueue("s1", _make_event(message="b"))

        # s2 should still accept
        result = mgr.enqueue("s2", _make_event(message="s2-a"))
        assert result is True


# ---------------------------------------------------------------------------
# Idle TTL eviction
# ---------------------------------------------------------------------------


class TestQueueManagerIdleEviction:
    def test_cleanup_idle_removes_stale_sessions(self) -> None:
        """Sessions not accessed within idle_ttl are evicted by cleanup_idle."""
        mgr = QueueManager(idle_ttl_seconds=60)
        mgr.enqueue("old_session", _make_event())

        # Simulate activity 2 hours ago
        mgr._last_active["old_session"] = time.time() - 7200

        evicted = mgr.cleanup_idle()
        assert evicted == 1
        assert mgr.depth("old_session") == 0

    def test_cleanup_idle_preserves_active_sessions(self) -> None:
        """Recently active sessions are NOT evicted."""
        mgr = QueueManager(idle_ttl_seconds=3600)
        mgr.enqueue("fresh", _make_event())
        # last_active is set to now by enqueue

        evicted = mgr.cleanup_idle()
        assert evicted == 0
        assert mgr.depth("fresh") == 1

    def test_cleanup_idle_returns_count_of_evicted(self) -> None:
        """cleanup_idle returns the number of sessions evicted."""
        mgr = QueueManager(idle_ttl_seconds=60)
        for key in ("s1", "s2", "s3"):
            mgr.enqueue(key, _make_event(session_key=key))
            mgr._last_active[key] = time.time() - 7200  # stale

        count = mgr.cleanup_idle()
        assert count == 3

    def test_cleanup_idle_accepts_custom_now(self) -> None:
        """cleanup_idle respects the ``now`` override for deterministic testing."""
        mgr = QueueManager(idle_ttl_seconds=60)
        mgr.enqueue("s1", _make_event())
        # Set last_active to a past timestamp
        mgr._last_active["s1"] = 1000.0

        # cleanup with now=2000 (1000s after last active, > 60s TTL)
        evicted = mgr.cleanup_idle(now=2000.0)
        assert evicted == 1

    def test_cleanup_idle_no_eviction_when_nothing_stale(self) -> None:
        """When no sessions are stale, cleanup_idle returns 0."""
        mgr = QueueManager(idle_ttl_seconds=3600)
        mgr.enqueue("s1", _make_event())
        assert mgr.cleanup_idle() == 0


# ---------------------------------------------------------------------------
# ensure_session
# ---------------------------------------------------------------------------


class TestQueueManagerEnsureSession:
    def test_ensure_session_is_idempotent(self) -> None:
        """ensure_session can be called multiple times without side effects."""
        mgr = QueueManager()
        mgr.ensure_session("key1")
        mgr.enqueue("key1", _make_event(message="m1"))
        mgr.ensure_session("key1")  # Second call — must not reset queue
        assert mgr.depth("key1") == 1
