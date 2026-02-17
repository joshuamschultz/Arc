"""Tests for screenshot tool — base64 PNG, resolution capping."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

from arcagent.modules.browser.config import BrowserConfig


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserScreenshot:
    """browser_screenshot tool."""

    async def test_screenshot_returns_base64(self) -> None:
        from arcagent.modules.browser.tools.screenshot import create_screenshot_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        # Mock Page.captureScreenshot response
        fake_png = base64.b64encode(b"fake-png-data").decode()
        cdp.send.return_value = {"data": fake_png}

        tools = create_screenshot_tools(cdp, config, bus)
        screenshot_tool = next(t for t in tools if t.name == "browser_screenshot")

        result = await screenshot_tool.execute()
        assert fake_png in result
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_screenshot_sends_clip_for_resolution_cap(self) -> None:
        from arcagent.modules.browser.tools.screenshot import create_screenshot_tools

        cdp = _make_cdp()
        config = BrowserConfig(
            security={"max_screenshot_width": 800, "max_screenshot_height": 600}  # type: ignore[arg-type]
        )
        bus = _make_bus()

        fake_png = base64.b64encode(b"fake").decode()
        cdp.send.return_value = {"data": fake_png}

        tools = create_screenshot_tools(cdp, config, bus)
        screenshot_tool = next(t for t in tools if t.name == "browser_screenshot")

        await screenshot_tool.execute()

        # Verify captureScreenshot was called with clip params
        call_args = cdp.send.call_args
        assert call_args[0][0] == "Page"
        assert call_args[0][1] == "captureScreenshot"
        params = call_args[0][2]
        assert params["clip"]["width"] == 800
        assert params["clip"]["height"] == 600
