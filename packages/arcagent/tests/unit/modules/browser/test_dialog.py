"""Tests for dialog tool — accept/dismiss/type for alert/confirm/prompt."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from arcagent.modules.browser.config import BrowserConfig


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value={})
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserHandleDialog:
    """browser_handle_dialog tool."""

    async def test_accept_dialog(self) -> None:
        from arcagent.modules.browser.tools.dialog import create_dialog_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_dialog_tools(cdp, config, bus)
        dialog_tool = next(t for t in tools if t.name == "browser_handle_dialog")

        result = await dialog_tool.execute(action="accept")
        assert "accept" in result.lower()
        cdp.send.assert_called_once_with("Page", "handleJavaScriptDialog", {"accept": True})

    async def test_dismiss_dialog(self) -> None:
        from arcagent.modules.browser.tools.dialog import create_dialog_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_dialog_tools(cdp, config, bus)
        dialog_tool = next(t for t in tools if t.name == "browser_handle_dialog")

        result = await dialog_tool.execute(action="dismiss")
        assert "dismiss" in result.lower()
        cdp.send.assert_called_once_with("Page", "handleJavaScriptDialog", {"accept": False})

    async def test_type_in_prompt(self) -> None:
        from arcagent.modules.browser.tools.dialog import create_dialog_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_dialog_tools(cdp, config, bus)
        dialog_tool = next(t for t in tools if t.name == "browser_handle_dialog")

        await dialog_tool.execute(action="accept", text="hello")
        cdp.send.assert_called_once_with(
            "Page",
            "handleJavaScriptDialog",
            {"accept": True, "promptText": "hello"},
        )
