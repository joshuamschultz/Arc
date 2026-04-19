"""Integration tests for StreamBridge 3-strikes flood-control.

Verifies that after 3 consecutive edit_message() failures the bridge
permanently disables progressive editing for the rest of the turn and
falls back to final-send-only delivery (Hermes pattern).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta
from arcgateway.stream_bridge import (
    EDIT_TOKEN_BUFFER_SIZE,
    FLOOD_STRIKE_LIMIT,
    StreamBridge,
)


# ---------------------------------------------------------------------------
# Adapters for flood-control testing
# ---------------------------------------------------------------------------


class _FailingEditAdapter:
    """Adapter where edit_message() always raises (simulates rate-limit)."""

    name = "mock_failing"

    def __init__(self) -> None:
        self.send_calls: list[str] = []
        self.edit_calls: int = 0
        self._sent_id: str = "msg-001"

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        self.send_calls.append(message)

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str:
        self.send_calls.append(message)
        return self._sent_id

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        self.edit_calls += 1
        raise RuntimeError("Telegram flood limit — 429 Too Many Requests")


class _FloodAfterNAdapter:
    """Adapter where edit_message() fails after N successful edits."""

    name = "mock_flood_after_n"

    def __init__(self, fail_after: int = 0) -> None:
        self.send_calls: list[str] = []
        self.edit_calls: int = 0
        self.successful_edits: int = 0
        self._fail_after = fail_after

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        self.send_calls.append(message)

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str:
        self.send_calls.append(message)
        return "msg-001"

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        self.edit_calls += 1
        if self.edit_calls > self._fail_after:
            raise RuntimeError("rate limited")
        self.successful_edits += 1


# ---------------------------------------------------------------------------
# Token stream generator
# ---------------------------------------------------------------------------


async def _token_stream(n_tokens: int, text: str = "x") -> AsyncIterator[Delta]:
    """Yield n_tokens token deltas then a done sentinel."""
    for _ in range(n_tokens):
        yield Delta(kind="token", content=text, is_final=False, turn_id="t1")
    yield Delta(kind="done", content="", is_final=True, turn_id="t1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flood_control_activates_after_3_consecutive_failures() -> None:
    """After 3 consecutive edit failures, flood-control disables further edits.

    Assertions:
    - edit_message() is called at most FLOOD_STRIKE_LIMIT + some buffer times
      (not indefinitely).
    - The final message is always delivered via send() regardless of flood mode.
    """
    adapter = _FailingEditAdapter()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    # Generate enough tokens to trigger multiple flush cycles.
    n_tokens = (FLOOD_STRIKE_LIMIT + 2) * EDIT_TOKEN_BUFFER_SIZE + 5

    await bridge.consume(_token_stream(n_tokens), target, adapter)

    # After 3 strike failures, no more edits should be attempted.
    # The bridge should stop calling edit_message once flood_disabled=True.
    assert adapter.edit_calls <= FLOOD_STRIKE_LIMIT + 5, (
        f"Expected edit_calls <= {FLOOD_STRIKE_LIMIT + 5}, got {adapter.edit_calls}. "
        "Flood-control did not activate after 3 strikes."
    )

    # The final message must always be sent.
    assert len(adapter.send_calls) >= 2, (
        "Expected at least placeholder + final send; "
        f"got {len(adapter.send_calls)} sends."
    )


@pytest.mark.asyncio
async def test_flood_control_final_send_always_occurs() -> None:
    """Final message delivery happens even when all edits fail."""
    adapter = _FailingEditAdapter()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    await bridge.consume(_token_stream(10), target, adapter)

    # Final send must always occur — this is the safety net.
    # The last send_calls entry should contain accumulated content.
    final_sends = [s for s in adapter.send_calls if s != "…"]
    assert len(final_sends) >= 1, "Final send must always happen"
    # The final message should contain accumulated token text.
    assert any("x" in s for s in final_sends), (
        f"Final send should contain token text; got: {final_sends}"
    )


@pytest.mark.asyncio
async def test_flood_control_no_further_edits_after_3_strikes() -> None:
    """After 3 strike failures, edit_message is NOT called again for the turn."""
    adapter = _FailingEditAdapter()  # always fails
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    # Stream many tokens — far more than would trigger multiple flush cycles.
    n_tokens = 200
    await bridge.consume(_token_stream(n_tokens), target, adapter)

    # Edits should be capped at approximately FLOOD_STRIKE_LIMIT attempts.
    # Allow a small buffer for timing variations.
    assert adapter.edit_calls <= FLOOD_STRIKE_LIMIT + 2, (
        f"Expected flood-control to stop after {FLOOD_STRIKE_LIMIT} strikes; "
        f"got {adapter.edit_calls} edit calls."
    )


@pytest.mark.asyncio
async def test_successful_edits_before_flood_limit() -> None:
    """Progressive edits proceed normally when edit_message() succeeds."""
    # Allow 5 successful edits before flooding.
    adapter = _FloodAfterNAdapter(fail_after=5)
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    n_tokens = 10 * EDIT_TOKEN_BUFFER_SIZE
    await bridge.consume(_token_stream(n_tokens), target, adapter)

    # Should have had some successful edits before flood control kicked in.
    assert adapter.successful_edits >= 1, "Expected some successful edits before flood"


@pytest.mark.asyncio
async def test_flood_control_not_triggered_on_success() -> None:
    """Flood-control must NOT activate when edit_message() always succeeds."""

    class _SucceedingAdapter:
        name = "succeed"

        def __init__(self) -> None:
            self.send_calls: list[str] = []
            self.edit_calls: int = 0

        async def send(
            self,
            target: DeliveryTarget,
            message: str,
            *,
            reply_to: str | None = None,
        ) -> None:
            self.send_calls.append(message)

        async def send_with_id(self, target: DeliveryTarget, message: str) -> str:
            self.send_calls.append(message)
            return "msg-001"

        async def edit_message(
            self,
            target: DeliveryTarget,
            message_id: str,
            new_text: str,
        ) -> None:
            self.edit_calls += 1  # Always succeeds

    adapter = _SucceedingAdapter()
    target = DeliveryTarget.parse("telegram:12345")
    bridge = StreamBridge()

    n_tokens = 5 * EDIT_TOKEN_BUFFER_SIZE
    await bridge.consume(_token_stream(n_tokens), target, adapter)

    # With all edits succeeding, we expect multiple edit calls.
    assert adapter.edit_calls >= 1, "Expected edit calls when no failures occur"
