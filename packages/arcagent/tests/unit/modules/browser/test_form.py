"""Tests for form fill tool — multi-field form filling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value=_FORM_AX_TREE)
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserFillForm:
    """browser_fill_form compound tool."""

    async def test_fill_multiple_fields(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.form import create_form_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        # Snapshot first to populate refs
        await ax.snapshot()

        # Reset mock for form interactions
        # Each field: DOM.focus + Input.insertText
        cdp.send.reset_mock()
        cdp.send.return_value = {}

        tools = create_form_tools(cdp, ax, config, bus)
        fill_tool = next(t for t in tools if t.name == "browser_fill_form")

        result = await fill_tool.execute(
            fields={"First Name": "John", "Last Name": "Doe"}
        )
        assert "2" in result  # Reports 2 fields filled

        # Verify Input.insertText was used (not char-by-char)
        cdp.send.assert_any_call("Input", "insertText", {"text": "John"})
        cdp.send.assert_any_call("Input", "insertText", {"text": "Doe"})

    async def test_fill_reports_not_found_fields(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.form import create_form_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.return_value = {}

        tools = create_form_tools(cdp, ax, config, bus)
        fill_tool = next(t for t in tools if t.name == "browser_fill_form")

        result = await fill_tool.execute(
            fields={"Nonexistent Field": "value"}
        )
        assert "not found" in result.lower() or "failed" in result.lower()
