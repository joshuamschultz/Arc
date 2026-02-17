"""Tests for read tools — page reading via accessibility snapshot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError

# Reuse the sample AX tree from test_accessibility
_SAMPLE_AX_TREE = {
    "nodes": [
        {
            "nodeId": "1",
            "role": {"value": "WebArea"},
            "name": {"value": "Test Page"},
            "backendDOMNodeId": 1,
            "childIds": ["2", "3"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"value": "heading"},
            "name": {"value": "Hello World"},
            "backendDOMNodeId": 10,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "3",
            "role": {"value": "button"},
            "name": {"value": "Click Me"},
            "backendDOMNodeId": 20,
            "childIds": [],
            "ignored": False,
        },
    ]
}


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value=_SAMPLE_AX_TREE)
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserReadPage:
    """browser_read_page tool."""

    async def test_read_page_returns_snapshot(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.read import create_read_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        tools = create_read_tools(cdp, ax, config, bus)
        read_tool = next(t for t in tools if t.name == "browser_read_page")

        result = await read_tool.execute()
        assert "[EXTERNAL WEB CONTENT]" in result
        assert "heading" in result
        assert "Hello World" in result
        assert "button" in result
        assert "Click Me" in result

    async def test_read_page_marks_content_as_external(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.read import create_read_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        tools = create_read_tools(cdp, ax, config, bus)
        read_tool = next(t for t in tools if t.name == "browser_read_page")

        result = await read_tool.execute()
        assert result.startswith("[EXTERNAL WEB CONTENT]")


class TestBrowserGetElementText:
    """browser_get_element_text tool."""

    async def test_get_element_text_by_ref(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.read import create_read_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        tools = create_read_tools(cdp, ax, config, bus)
        # First read page to populate refs
        read_tool = next(t for t in tools if t.name == "browser_read_page")
        await read_tool.execute()

        text_tool = next(t for t in tools if t.name == "browser_get_element_text")
        result = await text_tool.execute(ref=1)
        assert "Hello World" in result

    async def test_get_element_text_invalid_ref(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.read import create_read_tools

        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        tools = create_read_tools(cdp, ax, config, bus)
        # Read page first to populate refs
        read_tool = next(t for t in tools if t.name == "browser_read_page")
        await read_tool.execute()

        text_tool = next(t for t in tools if t.name == "browser_get_element_text")
        with pytest.raises(ElementNotFoundError):
            await text_tool.execute(ref=999)
