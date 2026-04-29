"""Integration tests — Slack Socket Mode replay deduplication (Hermes pattern).

T1.9: Inbound-event dedup table (platform, event_id) w/ 24h TTL.

These tests verify the core deduplication contract end-to-end using an
in-memory SQLite dedup store (no real Slack connection):

  test_socket_mode_replay_dedup
      Send the same event_id twice → on_message called once.
      Second delivery emits gateway.message.deduped log (replay audit).

  test_different_event_ids_both_processed
      Two events with different IDs → on_message called twice (no false-positive).

  test_dedup_ttl_sweep
      Insert a row with seen_at < now-86400. Sweep removes it.
      After sweep, the same event_id is accepted again (TTL worked).

  test_dedup_across_platforms
      Same event_id on different platforms → two separate entries (no cross-platform collision).

  test_no_event_id_always_dispatched
      Event with no client_msg_id and no event_id → dedup bypassed → always dispatched.
      This covers the edge case where Slack doesn't include an ID.

  test_dedup_store_record_or_skip_semantics
      Unit-level check: first record_or_skip returns False; second returns True.
"""

from __future__ import annotations

import time

from arcgateway.adapters.slack import SlackAdapter, _DedupStore
from arcgateway.executor import InboundEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    allowed_user_ids: list[str] | None = None,
) -> tuple[SlackAdapter, list[InboundEvent]]:
    """Build a SlackAdapter with in-memory dedup store."""
    received: list[InboundEvent] = []

    async def _on_message(event: InboundEvent) -> None:
        received.append(event)

    adapter = SlackAdapter(
        bot_token="xoxb-test-token-01",
        app_token="xapp-test-token-01",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else ["U_ALICE"],
        on_message=_on_message,
        dedup_db_path=None,  # in-memory for tests
    )
    return adapter, received


def _make_event(
    user: str = "U_ALICE",
    channel: str = "D_DIRECT",
    text: str = "hello",
    client_msg_id: str | None = "evt-001",
    event_id: str | None = None,
) -> dict:  # type: ignore[type-arg]
    """Build a minimal Slack message event payload."""
    payload: dict = {  # type: ignore[type-arg]
        "user": user,
        "channel": channel,
        "text": text,
    }
    if client_msg_id is not None:
        payload["client_msg_id"] = client_msg_id
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


# ---------------------------------------------------------------------------
# Replay deduplication — core contract
# ---------------------------------------------------------------------------


class TestSocketModeReplayDedup:
    async def test_socket_mode_replay_dedup(self) -> None:
        """Deliver the same event_id twice → on_message called exactly once.

        This is the primary T1.9 contract. Slack Socket Mode can redeliver
        events after a WebSocket reconnect; we must not process them twice.
        """
        adapter, received = _make_adapter()

        event = _make_event(client_msg_id="evt-replay-001")

        # First delivery → should be dispatched
        await adapter._handle_inbound(event)
        assert len(received) == 1, "First delivery must reach on_message"

        # Second delivery (replay) → must be deduplicated
        await adapter._handle_inbound(event)
        assert len(received) == 1, (
            "Replay must be deduplicated; on_message must not be called again"
        )

    async def test_different_event_ids_both_processed(self) -> None:
        """Two events with distinct IDs must both reach on_message (no false positive)."""
        adapter, received = _make_adapter()

        await adapter._handle_inbound(_make_event(client_msg_id="evt-A"))
        await adapter._handle_inbound(_make_event(client_msg_id="evt-B", text="world"))
        assert len(received) == 2

    async def test_replay_on_third_delivery_also_deduped(self) -> None:
        """A third delivery of the same event_id must also be deduplicated."""
        adapter, received = _make_adapter()
        event = _make_event(client_msg_id="evt-triple")

        for _ in range(3):
            await adapter._handle_inbound(event)

        assert len(received) == 1

    async def test_no_event_id_always_dispatched(self) -> None:
        """If no client_msg_id and no event_id, dedup is bypassed → always dispatched.

        This matches real Slack edge cases (some system messages lack IDs).
        Dedup only applies when an ID is present.
        """
        adapter, received = _make_adapter()

        event = _make_event(client_msg_id=None, event_id=None)

        await adapter._handle_inbound(event)
        await adapter._handle_inbound(event)
        # Both must be dispatched because there is no ID to deduplicate on
        assert len(received) == 2

    async def test_event_id_fallback_used(self) -> None:
        """When client_msg_id is absent, event_id in payload is used for dedup."""
        adapter, received = _make_adapter()

        event = _make_event(client_msg_id=None, event_id="env-ev-001")
        await adapter._handle_inbound(event)
        await adapter._handle_inbound(event)

        assert len(received) == 1, "event_id fallback should also trigger dedup"


# ---------------------------------------------------------------------------
# Cross-platform dedup isolation
# ---------------------------------------------------------------------------


class TestDedupCrossPlatform:
    def test_dedup_across_platforms(self) -> None:
        """Same event_id on different platforms → two separate entries.

        The PRIMARY KEY is (platform, event_id) — same ID on different
        platforms must NOT collide.
        """
        store = _DedupStore(db_path=None)

        # First record on "slack"
        is_dup_slack = store.record_or_skip("slack", "evt-shared-001")
        assert is_dup_slack is False

        # Same event_id on "telegram" must NOT be deduplicated
        is_dup_telegram = store.record_or_skip("telegram", "evt-shared-001")
        assert is_dup_telegram is False

        # Second record on same platform/id → duplicate
        is_dup_slack_again = store.record_or_skip("slack", "evt-shared-001")
        assert is_dup_slack_again is True

        store.close()


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------


class TestDedupTTLSweep:
    def test_dedup_ttl_sweep(self) -> None:
        """Row older than 24h is removed by sweep; event is re-accepted after sweep."""
        store = _DedupStore(db_path=None)

        # Insert a row directly with seen_at far in the past (>24h ago)
        ancient_seen_at = time.time() - (86400 + 3600)  # 25 hours ago
        store._conn.execute(
            "INSERT INTO event_dedup (platform, event_id, seen_at) VALUES (?, ?, ?)",
            ("slack", "evt-old-001", ancient_seen_at),
        )
        store._conn.commit()

        # Before sweep: the row exists, so record_or_skip returns True (duplicate)
        is_dup_before = store.record_or_skip("slack", "evt-old-001")
        assert is_dup_before is True, "Row should exist before sweep"

        # Remove the manually-inserted row and the one from record_or_skip
        store._conn.execute("DELETE FROM event_dedup WHERE event_id = 'evt-old-001'")
        store._conn.commit()
        # Re-insert only the ancient row (simulate a row that was never swept)
        store._conn.execute(
            "INSERT INTO event_dedup (platform, event_id, seen_at) VALUES (?, ?, ?)",
            ("slack", "evt-old-001", ancient_seen_at),
        )
        store._conn.commit()

        # Run sweep — should delete the ancient row
        deleted = store.sweep_expired()
        assert deleted >= 1, f"Sweep should remove at least 1 expired row, got {deleted}"

        # After sweep: same event_id is accepted as fresh
        is_dup_after = store.record_or_skip("slack", "evt-old-001")
        assert is_dup_after is False, "After sweep, event_id should be accepted as new"

        store.close()

    def test_sweep_leaves_fresh_rows(self) -> None:
        """Sweep must not remove rows less than 24h old."""
        store = _DedupStore(db_path=None)

        # Insert a fresh row
        store.record_or_skip("slack", "evt-fresh-001")

        # Run sweep — should not remove the fresh row
        deleted = store.sweep_expired()
        assert deleted == 0

        # Row must still be present (dedup still works)
        is_dup = store.record_or_skip("slack", "evt-fresh-001")
        assert is_dup is True

        store.close()

    def test_sweep_returns_count(self) -> None:
        """sweep_expired() returns the number of deleted rows."""
        store = _DedupStore(db_path=None)

        # Insert 3 ancient rows
        cutoff = time.time() - (86400 + 1)
        for i in range(3):
            store._conn.execute(
                "INSERT INTO event_dedup (platform, event_id, seen_at) VALUES (?, ?, ?)",
                ("slack", f"evt-old-{i}", cutoff),
            )
        store._conn.commit()

        deleted = store.sweep_expired()
        assert deleted == 3

        store.close()


# ---------------------------------------------------------------------------
# DedupStore unit-level contract
# ---------------------------------------------------------------------------


class TestDedupStoreSemantics:
    def test_record_or_skip_first_call_returns_false(self) -> None:
        """First record_or_skip call for a new event returns False (not a duplicate)."""
        store = _DedupStore(db_path=None)
        result = store.record_or_skip("slack", "evt-new")
        assert result is False
        store.close()

    def test_record_or_skip_second_call_returns_true(self) -> None:
        """Second record_or_skip call for the same event returns True (duplicate)."""
        store = _DedupStore(db_path=None)
        store.record_or_skip("slack", "evt-dup")
        result = store.record_or_skip("slack", "evt-dup")
        assert result is True
        store.close()

    def test_record_or_skip_primary_key_is_platform_and_event_id(self) -> None:
        """Different platform+event_id combos are independent entries."""
        store = _DedupStore(db_path=None)

        r1 = store.record_or_skip("slack", "id-1")
        r2 = store.record_or_skip("telegram", "id-1")
        r3 = store.record_or_skip("slack", "id-2")
        r4 = store.record_or_skip("slack", "id-1")  # duplicate

        assert r1 is False
        assert r2 is False  # same id, different platform → new row
        assert r3 is False  # same platform, different id → new row
        assert r4 is True  # exact match → duplicate

        store.close()
