"""Tests for interact tools — click, type, select, hover."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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
    cdp.send = AsyncMock()
    return cdp


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


class TestBrowserClick:
    """browser_click tool."""

    async def test_click_by_ref(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        # After snapshot, reset send mock for click interactions
        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {"model": {"content": [100, 100, 200, 100, 200, 200, 100, 200]}},  # DOM.getBoxModel
            {},  # Input.dispatchMouseEvent (mousePressed)
            {},  # Input.dispatchMouseEvent (mouseReleased)
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        click_tool = next(t for t in tools if t.name == "browser_click")

        result = await click_tool.execute(ref=2)  # Button "Submit"
        assert "Clicked" in result

    async def test_click_invalid_ref(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        tools = create_interact_tools(cdp, ax, config, bus)
        click_tool = next(t for t in tools if t.name == "browser_click")

        with pytest.raises(ElementNotFoundError):
            await click_tool.execute(ref=999)

    async def test_click_degenerate_box_model_raises(self) -> None:
        """Degenerate box model (no content) raises ElementNotFoundError."""
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {"model": {"content": []}},  # DOM.getBoxModel — empty content
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        click_tool = next(t for t in tools if t.name == "browser_click")

        with pytest.raises(ElementNotFoundError, match="bounding box"):
            await click_tool.execute(ref=2)


class TestBrowserType:
    """browser_type tool."""

    async def test_type_text_uses_insert_text(self) -> None:
        """Type uses Input.insertText (single call, not char-by-char)."""
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {},  # DOM.focus
            {},  # Input.insertText
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        type_tool = next(t for t in tools if t.name == "browser_type")

        result = await type_tool.execute(ref=1, text="hello")
        assert "Typed" in result
        assert "hello" in result

        # Verify Input.insertText was called (not char-by-char)
        cdp.send.assert_any_call("Input", "insertText", {"text": "hello"})

    async def test_type_redacts_return_value(self) -> None:
        """When redact_inputs is True, return value is redacted."""
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig(security={"redact_inputs": True})  # type: ignore[arg-type]
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {},  # DOM.focus
            {},  # Input.insertText
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        type_tool = next(t for t in tools if t.name == "browser_type")

        result = await type_tool.execute(ref=1, text="secret-password")
        assert "[REDACTED]" in result
        assert "secret-password" not in result


class TestBrowserSelect:
    """browser_select tool."""

    async def test_select_uses_arguments(self) -> None:
        """Select uses callFunctionOn with arguments (no JS injection)."""
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {"object": {"objectId": "obj-1"}},  # DOM.resolveNode
            {"result": {}},  # Runtime.callFunctionOn
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        select_tool = next(t for t in tools if t.name == "browser_select")

        result = await select_tool.execute(ref=3, value="UK")
        assert "Selected" in result

        # Verify callFunctionOn used arguments param (not f-string injection)
        call_args = cdp.send.call_args_list[1]
        params = call_args[0][2]  # Third positional arg
        assert "arguments" in params
        assert params["arguments"] == [{"value": "UK"}]


class TestBrowserHover:
    """browser_hover tool."""

    async def test_hover(self) -> None:
        from arcagent.modules.browser.accessibility import AccessibilityManager
        from arcagent.modules.browser.tools.interact import create_interact_tools

        cdp = _make_cdp()
        cdp.send.return_value = _SAMPLE_AX_TREE
        config = BrowserConfig()
        ax = AccessibilityManager(cdp, config)
        bus = _make_bus()

        await ax.snapshot()

        cdp.send.reset_mock()
        cdp.send.side_effect = [
            {"model": {"content": [100, 100, 200, 100, 200, 200, 100, 200]}},  # DOM.getBoxModel
            {},  # Input.dispatchMouseEvent (mouseMoved)
        ]

        tools = create_interact_tools(cdp, ax, config, bus)
        hover_tool = next(t for t in tools if t.name == "browser_hover")

        result = await hover_tool.execute(ref=2)
        assert "Hovered" in result
