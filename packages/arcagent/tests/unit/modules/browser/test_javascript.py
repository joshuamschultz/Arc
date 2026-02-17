"""Tests for JavaScript execution tool."""

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


class TestBrowserExecuteJS:
    """browser_execute_js tool."""

    async def test_execute_js_returns_result(self) -> None:
        from arcagent.modules.browser.tools.javascript import create_javascript_tools

        cdp = _make_cdp()
        cdp.send.return_value = {"result": {"type": "string", "value": "hello"}}
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_javascript_tools(cdp, config, bus)
        js_tool = next(t for t in tools if t.name == "browser_execute_js")

        result = await js_tool.execute(expression="'hello'")
        assert "hello" in result

    async def test_execute_js_handles_error(self) -> None:
        from arcagent.modules.browser.tools.javascript import create_javascript_tools

        cdp = _make_cdp()
        cdp.send.return_value = {
            "exceptionDetails": {
                "text": "ReferenceError: x is not defined"
            }
        }
        config = BrowserConfig()
        bus = _make_bus()

        tools = create_javascript_tools(cdp, config, bus)
        js_tool = next(t for t in tools if t.name == "browser_execute_js")

        result = await js_tool.execute(expression="x")
        assert "error" in result.lower() or "ReferenceError" in result
