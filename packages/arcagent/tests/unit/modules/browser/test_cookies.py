"""Tests for cookie tools — get/set cookies."""

from __future__ import annotations

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


class TestBrowserGetCookies:
    """browser_get_cookies tool."""

    async def test_get_cookies(self) -> None:
        from arcagent.modules.browser.tools.cookies import create_cookie_tools

        cdp = _make_cdp()
        cdp.send.return_value = {
            "cookies": [
                {"name": "session_id", "value": "abc123", "domain": ".example.com"},
                {"name": "theme", "value": "dark", "domain": ".example.com"},
            ]
        }
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_cookie_tools(cdp, config, bus)
        get_tool = next(t for t in tools if t.name == "browser_get_cookies")

        result = await get_tool.execute()
        assert "session_id" in result
        assert "theme" in result


class TestBrowserSetCookies:
    """browser_set_cookies tool."""

    async def test_set_cookies(self) -> None:
        from arcagent.modules.browser.tools.cookies import create_cookie_tools

        cdp = _make_cdp()
        cdp.send.return_value = {}
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_cookie_tools(cdp, config, bus)
        set_tool = next(t for t in tools if t.name == "browser_set_cookies")

        result = await set_tool.execute(
            cookies=[{"name": "token", "value": "xyz", "domain": ".example.com"}]
        )
        assert "Set 1 cookie" in result
