"""Tests for CDPClientManager — Chrome launch, WebSocket connection, lifecycle."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.browser.config import BrowserConnectionConfig
from arcagent.modules.browser.errors import CDPConnectionError


class TestCDPClientManagerConnect:
    """Connection lifecycle: launch Chrome, discover WS, connect."""

    async def test_connect_launches_chrome_and_discovers_ws(self) -> None:
        """connect() should launch Chrome subprocess and find WS URL."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig(remote_debugging_port=0)
        mgr = CDPClientManager(config)

        # Mock subprocess to return a Chrome process with WS URL on stderr
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None

        ws_url = "ws://127.0.0.1:9222/devtools/browser/abc-123"
        with (
            patch(
                "arcagent.modules.browser.cdp_client._find_chrome",
                return_value="/fake/path/to/chrome",
            ),
            patch(
                "arcagent.modules.browser.cdp_client.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_process,
            ) as mock_launch,
            patch(
                "arcagent.modules.browser.cdp_client.CDPClientManager._discover_ws_url",
                new_callable=AsyncMock,
                return_value=ws_url,
            ),
            patch(
                "arcagent.modules.browser.cdp_client.CDPClientManager._connect_ws",
                new_callable=AsyncMock,
            ),
            patch(
                "arcagent.modules.browser.cdp_client.CDPClientManager._enable_domains",
                new_callable=AsyncMock,
            ),
        ):
            await mgr.connect()
            mock_launch.assert_called_once()
            assert mgr.connected

        await mgr.disconnect()

    async def test_connect_with_external_cdp_url_skips_launch(self) -> None:
        """When cdp_url is set, skip Chrome launch and connect directly."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig(cdp_url="ws://localhost:9222/devtools/browser/abc")
        mgr = CDPClientManager(config)

        with (
            patch(
                "arcagent.modules.browser.cdp_client.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_launch,
            patch(
                "arcagent.modules.browser.cdp_client.CDPClientManager._connect_ws",
                new_callable=AsyncMock,
            ),
            patch(
                "arcagent.modules.browser.cdp_client.CDPClientManager._enable_domains",
                new_callable=AsyncMock,
            ),
        ):
            await mgr.connect()
            mock_launch.assert_not_called()
            assert mgr.connected

        await mgr.disconnect()

    async def test_connected_property_false_initially(self) -> None:
        """connected is False before connect() is called."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig()
        mgr = CDPClientManager(config)
        assert not mgr.connected


class TestCDPClientManagerDisconnect:
    """Graceful shutdown: close WS, stop Chrome, no zombies."""

    async def test_disconnect_closes_ws_and_kills_chrome(self) -> None:
        """disconnect() closes WebSocket and terminates Chrome process."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig()
        mgr = CDPClientManager(config)

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()

        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()

        mgr._process = mock_process
        mgr._ws = mock_ws
        mgr._connected = True

        await mgr.disconnect()

        mock_ws.close.assert_called_once()
        mock_process.terminate.assert_called_once()
        assert not mgr.connected

    async def test_disconnect_is_idempotent(self) -> None:
        """Calling disconnect() twice doesn't raise."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig()
        mgr = CDPClientManager(config)
        await mgr.disconnect()
        await mgr.disconnect()  # Should not raise


class TestCDPClientManagerSend:
    """CDP command send/receive."""

    async def test_send_returns_result(self) -> None:
        """send() sends a CDP command and returns the result."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig()
        mgr = CDPClientManager(config)

        mock_ws = AsyncMock()
        response = {"id": 1, "result": {"frameId": "abc"}}
        mock_ws.recv = AsyncMock(return_value=json.dumps(response))
        mock_ws.send = AsyncMock()

        mgr._ws = mock_ws
        mgr._connected = True
        mgr._cmd_id = 0

        result = await mgr.send("Page", "navigate", {"url": "https://example.com"})
        assert result == {"frameId": "abc"}

    async def test_send_raises_when_not_connected(self) -> None:
        """send() raises CDPConnectionError when not connected."""
        from arcagent.modules.browser.cdp_client import CDPClientManager

        config = BrowserConnectionConfig()
        mgr = CDPClientManager(config)

        with pytest.raises(CDPConnectionError, match="Not connected"):
            await mgr.send("Page", "navigate", {"url": "https://example.com"})
