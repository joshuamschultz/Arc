"""Tests for screenshot tool — base64 PNG, resolution capping."""

from __future__ import annotations

import base64
from collections.abc import Callable
from unittest.mock import AsyncMock

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import browser_screenshot
from arcagent.modules.browser.config import BrowserConfig


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


class TestBrowserScreenshot:
    """browser_screenshot tool."""

    async def test_screenshot_returns_base64(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        fake_png = base64.b64encode(b"fake-png-data").decode()
        cdp.send.return_value = {"data": fake_png}
        configure_browser(cdp=cdp)

        result = await browser_screenshot()
        assert fake_png in result
        assert "[EXTERNAL WEB CONTENT]" in result

    async def test_screenshot_sends_clip_for_resolution_cap(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        fake_png = base64.b64encode(b"fake").decode()
        cdp.send.return_value = {"data": fake_png}
        config = BrowserConfig(
            security={"max_screenshot_width": 800, "max_screenshot_height": 600}  # type: ignore[arg-type]
        )
        configure_browser(config, cdp=cdp)

        await browser_screenshot()

        call_args = cdp.send.call_args
        assert call_args[0][0] == "Page"
        assert call_args[0][1] == "captureScreenshot"
        params = call_args[0][2]
        assert params["clip"]["width"] == 800
        assert params["clip"]["height"] == 600
