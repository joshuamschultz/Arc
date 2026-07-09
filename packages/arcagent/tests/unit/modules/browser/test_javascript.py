"""Tests for JavaScript execution tool."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.capabilities import browser_execute_js


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock()
    return cdp


class TestBrowserExecuteJS:
    """browser_execute_js tool."""

    async def test_execute_js_returns_result(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        cdp.send.return_value = {"result": {"type": "string", "value": "hello"}}
        configure_browser(cdp=cdp)

        result = await browser_execute_js(expression="'hello'")
        assert "hello" in result

    async def test_execute_js_handles_error(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        cdp.send.return_value = {"exceptionDetails": {"text": "ReferenceError: x is not defined"}}
        configure_browser(cdp=cdp)

        result = await browser_execute_js(expression="x")
        assert "error" in result.lower() or "ReferenceError" in result
