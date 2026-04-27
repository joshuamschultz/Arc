"""Unit tests for DeliverySenderImpl.

Covers the untested lines in arcgateway/delivery.py (lines 175-203):
- send() dispatches to the registered adapter
- send() raises ValueError when no adapter is registered for platform
- send() accepts a raw address string or a DeliveryTarget object
- _resolve_target() raises TypeError for unsupported types
- register_adapter() stores adapter indexed by lowercase platform name
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from arcgateway.delivery import DeliverySenderImpl, DeliveryTarget


def _make_mock_adapter(platform: str) -> object:
    """Create a minimal mock adapter with an async send()."""

    class _Adapter:
        name = platform
        send = AsyncMock()

    return _Adapter()


class TestDeliverySenderImplRegister:
    def test_register_normalises_platform_lowercase(self) -> None:
        """register_adapter() normalises platform name to lowercase."""
        sender = DeliverySenderImpl()
        adapter = _make_mock_adapter("telegram")
        sender.register_adapter("TELEGRAM", adapter)  # type: ignore[arg-type]
        assert "telegram" in sender._adapters

    def test_register_stores_adapter(self) -> None:
        """register_adapter() stores the adapter keyed by platform."""
        sender = DeliverySenderImpl()
        adapter = _make_mock_adapter("slack")
        sender.register_adapter("slack", adapter)  # type: ignore[arg-type]
        assert sender._adapters["slack"] is adapter


class TestDeliverySenderImplSend:
    @pytest.mark.asyncio
    async def test_send_dispatches_to_adapter(self) -> None:
        """send() calls the registered adapter's send() method."""
        sender = DeliverySenderImpl()
        mock_send = AsyncMock()

        class _Adapter:
            name = "telegram"
            send = mock_send

        adapter = _Adapter()
        sender.register_adapter("telegram", adapter)  # type: ignore[arg-type]

        await sender.send("telegram:12345", "hello")

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        target: DeliveryTarget = call_args[0][0]
        assert target.platform == "telegram"
        assert target.chat_id == "12345"
        assert call_args[0][1] == "hello"

    @pytest.mark.asyncio
    async def test_send_accepts_delivery_target_object(self) -> None:
        """send() accepts a DeliveryTarget directly, not just strings."""
        sender = DeliverySenderImpl()
        mock_send = AsyncMock()

        class _Adapter:
            name = "slack"
            send = mock_send

        adapter = _Adapter()
        sender.register_adapter("slack", adapter)  # type: ignore[arg-type]

        target = DeliveryTarget.parse("slack:D456")
        await sender.send(target, "hi there")

        mock_send.assert_called_once()
        call_target = mock_send.call_args[0][0]
        assert call_target.chat_id == "D456"

    @pytest.mark.asyncio
    async def test_send_raises_when_no_adapter_registered(self) -> None:
        """send() raises ValueError when no adapter is registered for the platform."""
        sender = DeliverySenderImpl()

        with pytest.raises(ValueError, match="No adapter registered"):
            await sender.send("telegram:99999", "message with no adapter")

    @pytest.mark.asyncio
    async def test_send_with_thread_id_passes_target(self) -> None:
        """send() passes the thread_id through to the adapter."""
        sender = DeliverySenderImpl()
        mock_send = AsyncMock()

        class _Adapter:
            name = "slack"
            send = mock_send

        adapter = _Adapter()
        sender.register_adapter("slack", adapter)  # type: ignore[arg-type]

        await sender.send("slack:C123:T789", "threaded reply")

        call_target: DeliveryTarget = mock_send.call_args[0][0]
        assert call_target.thread_id == "T789"


class TestDeliverySenderImplResolveTarget:
    def test_resolve_target_raises_for_unsupported_type(self) -> None:
        """_resolve_target() raises TypeError for types other than str/DeliveryTarget."""
        with pytest.raises(TypeError, match="Cannot resolve delivery target"):
            DeliverySenderImpl._resolve_target(12345)  # type: ignore[arg-type]

    def test_resolve_target_passes_through_delivery_target(self) -> None:
        """_resolve_target() returns a DeliveryTarget unchanged."""
        t = DeliveryTarget.parse("telegram:111")
        result = DeliverySenderImpl._resolve_target(t)
        assert result is t

    def test_resolve_target_parses_string(self) -> None:
        """_resolve_target() parses a string address into DeliveryTarget."""
        result = DeliverySenderImpl._resolve_target("telegram:222")
        assert isinstance(result, DeliveryTarget)
        assert result.chat_id == "222"
