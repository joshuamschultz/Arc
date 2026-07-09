"""Tests for cookie tools — get/set cookies."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import (
    browser_get_cookies,
    browser_set_cookies,
)


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


class TestBrowserGetCookies:
    """browser_get_cookies tool."""

    async def test_get_cookies(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.return_value = {
            "cookies": [
                {"name": "session_id", "value": "abc123", "domain": ".example.com"},
                {"name": "theme", "value": "dark", "domain": ".example.com"},
            ]
        }
        configure_browser(cdp=cdp)

        result = await browser_get_cookies()
        assert "session_id" in result
        assert "theme" in result


class TestBrowserSetCookies:
    """browser_set_cookies tool."""

    async def test_set_cookies(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        cdp.send.return_value = {}
        configure_browser(cdp=cdp)

        result = await browser_set_cookies(
            cookies=[{"name": "token", "value": "xyz", "domain": ".example.com"}]
        )
        assert "Set 1 cookie" in result
