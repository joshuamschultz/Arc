"""Tests for BrowserSession — lifecycle, tool operations, audit events.

All Playwright objects are mocked. No real browser is launched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.errors import (
    BrowserNotAvailableError,
    ElementNotFoundError,
    NavigationFailedError,
)
from arcagent.modules.browser.session import BrowserSession, _format_ax_tree


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_page() -> MagicMock:
    """Create a mock Playwright Page."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.inner_text = AsyncMock(return_value="hello world")
    page.fill = AsyncMock()
    page.close = AsyncMock()

    # Mock accessibility tree
    accessibility = MagicMock()
    accessibility.snapshot = AsyncMock(
        return_value={
            "role": "WebArea",
            "name": "Test Page",
            "children": [
                {"role": "button", "name": "Submit", "children": []},
                {"role": "link", "name": "Home", "children": []},
            ],
        }
    )
    page.accessibility = accessibility
    return page


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


class TestBrowserSessionInit:
    """BrowserSession initialises with correct defaults."""

    def test_auto_generates_session_id(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        assert s.session_id
        assert len(s.session_id) > 0

    def test_custom_session_id_preserved(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page, session_id="test-session-123")
        assert s.session_id == "test-session-123"

    def test_is_closed_initially_false(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        assert not s.is_closed

    def test_default_mode_and_provider(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        assert s._mode == "local"
        assert s._provider == "local"

    def test_custom_mode_and_provider(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page, mode="remote", provider="browserbase")
        assert s._mode == "remote"
        assert s._provider == "browserbase"


# ---------------------------------------------------------------------------
# emit_created
# ---------------------------------------------------------------------------


class TestEmitCreated:
    async def test_emits_session_created_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="s1", mode="remote", provider="bb", audit_fn=capture)
        await s.emit_created()

        assert len(events) == 1
        name, payload = events[0]
        assert name == "browser.session.created"
        assert payload["session_id"] == "s1"
        assert payload["mode"] == "remote"
        assert payload["provider"] == "bb"


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------


class TestNavigate:
    async def test_calls_page_goto(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.navigate("https://example.com")
        page.goto.assert_called_once_with("https://example.com")

    async def test_emits_navigate_audit_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="nav-1", audit_fn=capture)
        await s.navigate("https://example.com")

        names = [e[0] for e in events]
        assert "browser.navigate" in names
        nav_event = next(e[1] for e in events if e[0] == "browser.navigate")
        assert nav_event["url"] == "https://example.com"
        assert nav_event["session_id"] == "nav-1"
        assert "duration_ms" in nav_event

    async def test_raises_navigation_failed_on_error(self) -> None:
        page = _make_page()
        page.goto.side_effect = Exception("net::ERR_NAME_NOT_RESOLVED")
        s = BrowserSession(page=page)
        with pytest.raises(NavigationFailedError) as exc_info:
            await s.navigate("https://bad.invalid")
        assert "bad.invalid" in exc_info.value.details["url"]

    async def test_raises_when_closed(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.close()
        with pytest.raises(BrowserNotAvailableError):
            await s.navigate("https://example.com")


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------


class TestClick:
    async def test_calls_page_click(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.click("#submit-btn")
        page.click.assert_called_once_with("#submit-btn")

    async def test_emits_click_audit_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="clk-1", audit_fn=capture)
        await s.click("#btn")

        names = [e[0] for e in events]
        assert "browser.click" in names
        click_event = next(e[1] for e in events if e[0] == "browser.click")
        assert click_event["selector"] == "#btn"
        # Element content must never appear in audit payload
        assert "content" not in click_event
        assert "text" not in click_event

    async def test_raises_element_not_found_on_error(self) -> None:
        page = _make_page()
        page.click.side_effect = Exception("No element found")
        s = BrowserSession(page=page)
        with pytest.raises(ElementNotFoundError):
            await s.click(".missing")


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    async def test_returns_formatted_accessibility_tree(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        result = await s.snapshot()
        assert "WebArea" in result or "button" in result or "Submit" in result

    async def test_returns_empty_string_on_error(self) -> None:
        page = _make_page()
        page.accessibility.snapshot.side_effect = Exception("AX error")
        s = BrowserSession(page=page)
        result = await s.snapshot()
        assert result == ""


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


class TestExtract:
    async def test_returns_text_content(self) -> None:
        page = _make_page()
        page.inner_text = AsyncMock(return_value="hello world")
        s = BrowserSession(page=page)
        result = await s.extract("h1")
        assert result == "hello world"

    async def test_emits_extract_audit_event_with_size(self) -> None:
        page = _make_page()
        page.inner_text = AsyncMock(return_value="abc")
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="ext-1", audit_fn=capture)
        await s.extract(".content")

        names = [e[0] for e in events]
        assert "browser.extract" in names
        ext_event = next(e[1] for e in events if e[0] == "browser.extract")
        assert ext_event["selector"] == ".content"
        assert ext_event["content_size_bytes"] == 3
        # Extracted content must NOT appear in audit payload
        assert "content" not in ext_event
        assert "text" not in ext_event

    async def test_raises_element_not_found_on_error(self) -> None:
        page = _make_page()
        page.inner_text.side_effect = Exception("No element")
        s = BrowserSession(page=page)
        with pytest.raises(ElementNotFoundError):
            await s.extract(".missing")


# ---------------------------------------------------------------------------
# type_text
# ---------------------------------------------------------------------------


class TestTypeText:
    async def test_calls_page_fill(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.type_text("#name", "John Doe")
        page.fill.assert_called_once_with("#name", "John Doe")

    async def test_raises_element_not_found_on_error(self) -> None:
        page = _make_page()
        page.fill.side_effect = Exception("No element")
        s = BrowserSession(page=page)
        with pytest.raises(ElementNotFoundError):
            await s.type_text(".missing", "text")


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_marks_session_closed(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.close()
        assert s.is_closed

    async def test_close_emits_session_closed_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="cls-1", mode="remote", audit_fn=capture)
        await s.close()

        names = [e[0] for e in events]
        assert "browser.session.closed" in names
        closed_event = next(e[1] for e in events if e[0] == "browser.session.closed")
        assert closed_event["session_id"] == "cls-1"
        assert closed_event["mode"] == "remote"

    async def test_close_idempotent(self) -> None:
        """Second close() is a no-op, not an error."""
        page = _make_page()
        s = BrowserSession(page=page)
        await s.close()
        await s.close()  # must not raise
        assert page.close.call_count == 1

    async def test_operation_after_close_raises_browser_not_available(self) -> None:
        page = _make_page()
        s = BrowserSession(page=page)
        await s.close()
        with pytest.raises(BrowserNotAvailableError):
            await s.click("#btn")


# ---------------------------------------------------------------------------
# Accessibility tree formatter
# ---------------------------------------------------------------------------


class TestFormatAxTree:
    def test_formats_simple_tree(self) -> None:
        tree = {"role": "button", "name": "Submit", "children": []}
        result = _format_ax_tree(tree)
        assert "button" in result
        assert "Submit" in result

    def test_formats_nested_tree(self) -> None:
        tree = {
            "role": "WebArea",
            "name": "Page",
            "children": [
                {"role": "link", "name": "Home", "children": []},
            ],
        }
        result = _format_ax_tree(tree)
        assert "WebArea" in result
        assert "link" in result
        assert "Home" in result

    def test_returns_empty_string_for_none(self) -> None:
        result = _format_ax_tree(None)
        assert result == ""

    def test_handles_node_without_name(self) -> None:
        tree = {"role": "paragraph", "children": []}
        result = _format_ax_tree(tree)
        assert "paragraph" in result
