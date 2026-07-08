"""Tests for dialog tool — accept/dismiss/type for alert/confirm/prompt."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import browser_handle_dialog


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value={})
    return cdp


class TestBrowserHandleDialog:
    """browser_handle_dialog tool."""

    async def test_accept_dialog(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        configure_browser(cdp=cdp)

        result = await browser_handle_dialog(action="accept")
        assert "accept" in result.lower()
        cdp.send.assert_called_once_with("Page", "handleJavaScriptDialog", {"accept": True})

    async def test_dismiss_dialog(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        configure_browser(cdp=cdp)

        result = await browser_handle_dialog(action="dismiss")
        assert "dismiss" in result.lower()
        cdp.send.assert_called_once_with("Page", "handleJavaScriptDialog", {"accept": False})

    async def test_type_in_prompt(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        configure_browser(cdp=cdp)

        await browser_handle_dialog(action="accept", text="hello")
        cdp.send.assert_called_once_with(
            "Page",
            "handleJavaScriptDialog",
            {"accept": True, "promptText": "hello"},
        )
