"""Tests for read tools — page reading via accessibility snapshot."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.capabilities import (
    browser_get_element_text,
    browser_read_page,
)
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError

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


class TestBrowserReadPage:
    """browser_read_page tool."""

    async def test_read_page_returns_snapshot(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_read_page()
        assert "[EXTERNAL WEB CONTENT]" in result
        assert "heading" in result
        assert "Hello World" in result
        assert "button" in result
        assert "Click Me" in result

    async def test_read_page_marks_content_as_external(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_read_page()
        assert result.startswith("[EXTERNAL WEB CONTENT]")


class TestBrowserGetElementText:
    """browser_get_element_text tool."""

    async def test_get_element_text_by_ref(self, configure_browser: Callable[..., _State]) -> None:
        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        configure_browser(config, cdp=cdp, ax=ax)

        # First read page to populate refs
        await browser_read_page()

        result = await browser_get_element_text(ref=1)
        assert "Hello World" in result

    async def test_get_element_text_invalid_ref(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        cdp = _make_cdp()
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        configure_browser(config, cdp=cdp, ax=ax)

        # Read page first to populate refs
        await browser_read_page()

        with pytest.raises(ElementNotFoundError):
            await browser_get_element_text(ref=999)
