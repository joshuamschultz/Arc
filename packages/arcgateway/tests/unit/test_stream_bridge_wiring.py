"""Unit tests for StreamBridge — SPEC-018 Wave B1 perf fixes.

Covers:
  - test_no_per_edit_audit_events      — gateway.message.edited no longer emitted
  - test_turn_summary_emitted          — gateway.message.turn_summary emitted once
  - test_turn_summary_shape            — summary has target, edit_count, flood_disabled
  - test_accumulated_string_is_correct — list-join produces correct full text
  - test_flood_disabled_in_summary     — flood_disabled=True when 3-strikes hit
  - test_final_sent_still_emitted      — gateway.message.final_sent still fires
  - test_message_sent_still_emitted    — gateway.message.sent still fires
  - test_no_summary_on_zero_edits      — no turn_summary when flood never tried
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta
from arcgateway.stream_bridge import FLOOD_STRIKE_LIMIT, StreamBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _delta(content: str, kind: str = "token", is_final: bool = False) -> Delta:
    return Delta(kind=kind, content=content, is_final=is_final)


async def _stream(*items: Delta) -> AsyncIterator[Delta]:
    for item in items:
        yield item


def _make_adapter(
    edit_raises: bool = False,
    ts: str | None = "ts-001",
) -> MagicMock:
    """Build a mock platform adapter."""
    adapter = MagicMock()
    adapter.send_with_id = AsyncMock(return_value=ts)
    adapter.send = AsyncMock()
    if edit_raises:
        adapter.edit_message = AsyncMock(side_effect=Exception("rate limited"))
    else:
        adapter.edit_message = AsyncMock()
    return adapter


def _target() -> DeliveryTarget:
    return DeliveryTarget.parse("slack:D001")


# ---------------------------------------------------------------------------
# Per-edit audit events must NOT be emitted
# ---------------------------------------------------------------------------


async def test_no_per_edit_audit_events() -> None:
    """gateway.message.edited must never be emitted (removed in Wave B1)."""
    audit_events: list[str] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        audit_events.append(event_name)

    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    # Generate enough tokens to trigger at least one edit flush (>= EDIT_TOKEN_BUFFER_SIZE).
    tokens = [_delta(f"t{i}") for i in range(25)] + [_delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert "gateway.message.edited" not in audit_events, (
        "gateway.message.edited was emitted but should have been removed in Wave B1"
    )


# ---------------------------------------------------------------------------
# Per-turn summary emitted once
# ---------------------------------------------------------------------------


async def test_turn_summary_emitted() -> None:
    """gateway.message.turn_summary must be emitted exactly once per turn."""
    audit_events: list[str] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        audit_events.append(event_name)

    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    tokens = [_delta(f"t{i}") for i in range(25)] + [_delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    summary_count = audit_events.count("gateway.message.turn_summary")
    assert summary_count == 1, (
        f"Expected exactly 1 gateway.message.turn_summary, got {summary_count}"
    )


# ---------------------------------------------------------------------------
# Turn summary shape
# ---------------------------------------------------------------------------


async def test_turn_summary_shape() -> None:
    """gateway.message.turn_summary must have target, edit_count, flood_disabled."""
    captured: list[dict[str, Any]] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        if event_name == "gateway.message.turn_summary":
            captured.append(data)

    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    tokens = [_delta(f"t{i}") for i in range(25)] + [_delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert len(captured) == 1
    summary = captured[0]
    assert "target" in summary
    assert "edit_count" in summary
    assert "flood_disabled" in summary
    assert isinstance(summary["edit_count"], int)
    assert isinstance(summary["flood_disabled"], bool)
    assert summary["flood_disabled"] is False
    assert summary["edit_count"] >= 1


# ---------------------------------------------------------------------------
# Accumulated string correctness
# ---------------------------------------------------------------------------


async def test_accumulated_string_is_correct() -> None:
    """The full text delivered via final send must be the join of all tokens."""
    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    words = ["hello", " ", "world", " ", "from", " ", "arc"]
    tokens = [_delta(w) for w in words] + [_delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit"):
        await bridge.consume(_stream(*tokens), target, adapter)

    expected = "".join(words)
    adapter.send.assert_called_once()
    actual_text = adapter.send.call_args.args[1]
    assert actual_text == expected, f"Expected {expected!r}, got {actual_text!r}"


# ---------------------------------------------------------------------------
# flood_disabled reflected in turn summary
# ---------------------------------------------------------------------------


async def test_flood_disabled_in_summary() -> None:
    """When 3-strikes fires, turn_summary must have flood_disabled=True."""
    captured: list[dict[str, Any]] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        if event_name == "gateway.message.turn_summary":
            captured.append(data)

    bridge = StreamBridge()
    # Adapter whose edit_message always fails → triggers 3-strikes.
    adapter = _make_adapter(edit_raises=True)
    target = _target()

    # Send enough tokens to hit FLOOD_STRIKE_LIMIT edit attempts.
    # Each flush attempt requires >= EDIT_TOKEN_BUFFER_SIZE tokens.
    tokens = [_delta(f"t{i}") for i in range(FLOOD_STRIKE_LIMIT * 25)] + [
        _delta("", is_final=True)
    ]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert len(captured) == 1
    assert captured[0]["flood_disabled"] is True


# ---------------------------------------------------------------------------
# gateway.message.final_sent still emitted
# ---------------------------------------------------------------------------


async def test_final_sent_still_emitted() -> None:
    """gateway.message.final_sent must still be emitted (unchanged in Wave B1)."""
    audit_events: list[str] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        audit_events.append(event_name)

    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    tokens = [_delta("hello"), _delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert "gateway.message.final_sent" in audit_events


# ---------------------------------------------------------------------------
# gateway.message.sent still emitted
# ---------------------------------------------------------------------------


async def test_message_sent_still_emitted() -> None:
    """gateway.message.sent must still be emitted on placeholder send."""
    audit_events: list[str] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        audit_events.append(event_name)

    bridge = StreamBridge()
    adapter = _make_adapter()
    target = _target()

    tokens = [_delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert "gateway.message.sent" in audit_events


# ---------------------------------------------------------------------------
# No turn_summary when zero edit attempts
# ---------------------------------------------------------------------------


async def test_no_summary_on_zero_edits() -> None:
    """turn_summary must NOT be emitted when no edits were attempted (short stream)."""
    audit_events: list[str] = []

    def fake_emit_audit(event_name: str, data: dict[str, Any]) -> None:
        audit_events.append(event_name)

    bridge = StreamBridge()
    # Adapter with no ts → edit_message path never entered.
    adapter = _make_adapter(ts=None)
    target = _target()

    # Only a few tokens — well under EDIT_TOKEN_BUFFER_SIZE.
    tokens = [_delta("hi"), _delta("", is_final=True)]

    with patch("arcgateway.stream_bridge._audit", fake_emit_audit):
        await bridge.consume(_stream(*tokens), target, adapter)

    assert "gateway.message.turn_summary" not in audit_events
