"""Tests for form fill tool — multi-field form filling."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.capabilities import browser_fill_form
from arcagent.modules.browser.config import BrowserConfig

_FORM_AX_TREE = {
    "nodes": [
        {
            "nodeId": "1",
            "role": {"value": "WebArea"},
            "name": {"value": "Form"},
            "backendDOMNodeId": 1,
            "childIds": ["2", "3", "4"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"value": "textbox"},
            "name": {"value": "First Name"},
            "value": {"value": ""},
            "backendDOMNodeId": 10,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "3",
            "role": {"value": "textbox"},
            "name": {"value": "Last Name"},
            "value": {"value": ""},
            "backendDOMNodeId": 20,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "4",
            "role": {"value": "textbox"},
            "name": {"value": "Email"},
            "value": {"value": ""},
            "backendDOMNodeId": 30,
            "childIds": [],
            "ignored": False,
        },
    ]
}


async def _snapshot(config: BrowserConfig) -> tuple[AsyncMock, AccessibilityManager]:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value=_FORM_AX_TREE)
    ax = AccessibilityManager(cdp, config)
    await ax.snapshot()
    cdp.send.reset_mock()
    cdp.send.return_value = {}
    return cdp, ax


class TestBrowserFillForm:
    """browser_fill_form compound tool."""

    async def test_fill_multiple_fields(self, configure_browser: Callable[..., _State]) -> None:
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_fill_form(fields={"First Name": "John", "Last Name": "Doe"})
        assert "2" in result  # Reports 2 fields filled

        cdp.send.assert_any_call("Input", "insertText", {"text": "John"})
        cdp.send.assert_any_call("Input", "insertText", {"text": "Doe"})

    async def test_fill_reports_not_found_fields(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_fill_form(fields={"Nonexistent Field": "value"})
        assert "not found" in result.lower() or "failed" in result.lower()
