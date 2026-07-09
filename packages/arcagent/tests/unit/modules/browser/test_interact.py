"""Tests for interact tools — click, type, select, hover."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.browser._runtime import _State
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.capabilities import (
    browser_click,
    browser_hover,
    browser_select,
    browser_type,
)
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import ElementNotFoundError

_SAMPLE_AX_TREE = {
    "nodes": [
        {
            "nodeId": "1",
            "role": {"value": "WebArea"},
            "name": {"value": "Test"},
            "backendDOMNodeId": 1,
            "childIds": ["2", "3", "4"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"value": "textbox"},
            "name": {"value": "Email"},
            "value": {"value": ""},
            "backendDOMNodeId": 10,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "3",
            "role": {"value": "button"},
            "name": {"value": "Submit"},
            "backendDOMNodeId": 20,
            "childIds": [],
            "ignored": False,
        },
        {
            "nodeId": "4",
            "role": {"value": "combobox"},
            "name": {"value": "Country"},
            "value": {"value": "US"},
            "backendDOMNodeId": 30,
            "childIds": [],
            "ignored": False,
        },
    ]
}


def _make_cdp() -> AsyncMock:
    cdp = AsyncMock()
    cdp.send = AsyncMock(return_value=_SAMPLE_AX_TREE)
    return cdp


async def _snapshot(config: BrowserConfig) -> tuple[AsyncMock, AccessibilityManager]:
    """Build a CDP + AX manager and populate refs from the sample tree."""
    cdp = _make_cdp()
    ax = AccessibilityManager(cdp, config)
    await ax.snapshot()
    cdp.send.reset_mock()
    return cdp, ax


class TestBrowserClick:
    """browser_click tool."""

    async def test_click_by_ref(self, configure_browser: Callable[..., _State]) -> None:
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {"model": {"content": [100, 100, 200, 100, 200, 200, 100, 200]}},  # DOM.getBoxModel
            {},  # Input.dispatchMouseEvent (mousePressed)
            {},  # Input.dispatchMouseEvent (mouseReleased)
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_click(ref=2)  # Button "Submit"
        assert "Clicked" in result

    async def test_click_invalid_ref(self, configure_browser: Callable[..., _State]) -> None:
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        configure_browser(config, cdp=cdp, ax=ax)

        with pytest.raises(ElementNotFoundError):
            await browser_click(ref=999)

    async def test_click_degenerate_box_model_raises(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Degenerate box model (no content) raises ElementNotFoundError."""
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {"model": {"content": []}},  # DOM.getBoxModel — empty content
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        with pytest.raises(ElementNotFoundError, match="bounding box"):
            await browser_click(ref=2)


class TestBrowserType:
    """browser_type tool."""

    async def test_type_text_uses_insert_text(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Type uses Input.insertText (single call, not char-by-char)."""
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {},  # DOM.focus
            {},  # Input.insertText
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_type(ref=1, text="hello")
        assert "Typed" in result
        assert "hello" in result
        cdp.send.assert_any_call("Input", "insertText", {"text": "hello"})

    async def test_type_redacts_return_value(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """When redact_inputs is True, return value is redacted."""
        config = BrowserConfig(security={"redact_inputs": True})  # type: ignore[arg-type]
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {},  # DOM.focus
            {},  # Input.insertText
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_type(ref=1, text="secret-password")
        assert "[REDACTED]" in result
        assert "secret-password" not in result


class TestBrowserSelect:
    """browser_select tool."""

    async def test_select_uses_arguments(
        self, configure_browser: Callable[..., _State]
    ) -> None:
        """Select uses callFunctionOn with arguments (no JS injection)."""
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {"object": {"objectId": "obj-1"}},  # DOM.resolveNode
            {"result": {}},  # Runtime.callFunctionOn
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_select(ref=3, value="UK")
        assert "Selected" in result

        call_args = cdp.send.call_args_list[1]
        params = call_args[0][2]  # Third positional arg
        assert "arguments" in params
        assert params["arguments"] == [{"value": "UK"}]


class TestBrowserHover:
    """browser_hover tool."""

    async def test_hover(self, configure_browser: Callable[..., _State]) -> None:
        config = BrowserConfig()
        cdp, ax = await _snapshot(config)
        cdp.send.side_effect = [
            {"model": {"content": [100, 100, 200, 100, 200, 200, 100, 200]}},  # DOM.getBoxModel
            {},  # Input.dispatchMouseEvent (mouseMoved)
        ]
        configure_browser(config, cdp=cdp, ax=ax)

        result = await browser_hover(ref=2)
        assert "Hovered" in result
