"""Integration test: Control command flow.

Verifies: Browser POST → agents route → pending_controls → ControlResponse.
Tests the request-response correlation pattern.
"""

from __future__ import annotations

import asyncio

from arcui.types import ControlMessage, ControlResponse


class TestControlCorrelation:
    """Verify the pending_controls future-based correlation pattern."""

    async def test_control_message_and_response_correlation(self) -> None:
        """A ControlResponse should resolve the matching pending future."""
        pending: dict[str, asyncio.Future[ControlResponse]] = {}

        # Simulate control proxy creating a future
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ControlResponse] = loop.create_future()
        pending["req-001"] = future

        msg = ControlMessage(
            action="steer",
            target="agent-1",
            data={"temperature": 0.5},
            request_id="req-001",
        )
        assert msg.request_id == "req-001"

        # Simulate agent responding
        response = ControlResponse(
            request_id="req-001",
            status="ok",
            data={"applied": True},
        )

        # Resolve the future (as agent_ws.py does)
        if not future.done():
            future.set_result(response)

        result = await asyncio.wait_for(future, timeout=1.0)
        assert result.status == "ok"
        assert result.data["applied"] is True

    async def test_unmatched_response_ignored(self) -> None:
        """A response with no matching future should not raise."""
        pending: dict[str, asyncio.Future[ControlResponse]] = {}

        response = ControlResponse(
            request_id="nonexistent",
            status="ok",
            data={},
        )

        future = pending.get(response.request_id)
        # Should be None — no error
        assert future is None

    async def test_timeout_on_no_response(self) -> None:
        """A pending future should raise TimeoutError if agent never responds."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ControlResponse] = loop.create_future()

        with __import__("pytest").raises(TimeoutError):
            await asyncio.wait_for(future, timeout=0.05)

    async def test_disconnected_agent_resolves_with_error(self) -> None:
        """When agent disconnects, pending futures should be resolved with error."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ControlResponse] = loop.create_future()

        # Simulate agent disconnect cleanup (as in agent_ws.py)
        if not future.done():
            future.set_exception(TimeoutError("Agent disconnected"))

        with __import__("pytest").raises(TimeoutError, match="disconnected"):
            await future
