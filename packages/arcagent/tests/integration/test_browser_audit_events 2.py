"""Integration test: navigate → audit event emitted (T4.9).

Verifies the full audit event chain from BrowserSession.navigate()
through to the audit callback. Uses a mocked Playwright Page so no
real browser process is launched.

This test is intentionally placed in integration/ (not unit/) because
it exercises the collaboration between BrowserSession, audit_fn, and
the audit event payload contract specified in MODULE.yaml.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser.session import BrowserSession


def _make_page(navigate_delay_s: float = 0.0) -> MagicMock:
    """Create a mock Playwright Page with configurable navigation timing."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.inner_text = AsyncMock(return_value="content")
    page.fill = AsyncMock()
    page.close = AsyncMock()

    accessibility = MagicMock()
    accessibility.snapshot = AsyncMock(return_value=None)
    page.accessibility = accessibility
    return page


class TestNavigateAuditEvent:
    """navigate() emits browser.navigate with correct payload."""

    async def test_navigate_emits_audit_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(
            page=page,
            session_id="audit-test-1",
            mode="remote",
            provider="browserbase",
            audit_fn=capture,
        )
        await s.navigate("https://example.com/page")

        assert any(e[0] == "browser.navigate" for e in events), (
            "Expected browser.navigate event to be emitted"
        )

    async def test_navigate_audit_payload_contains_url(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="url-test", audit_fn=capture)
        await s.navigate("https://example.com/secret-path?q=test")

        nav = next(e[1] for e in events if e[0] == "browser.navigate")
        # URL must be logged verbatim — no hashing (federal NIST AU-3 requirement)
        assert nav["url"] == "https://example.com/secret-path?q=test"

    async def test_navigate_audit_payload_contains_session_id(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="sid-123", audit_fn=capture)
        await s.navigate("https://example.com")

        nav = next(e[1] for e in events if e[0] == "browser.navigate")
        assert nav["session_id"] == "sid-123"

    async def test_navigate_audit_payload_contains_duration_ms(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, audit_fn=capture)
        await s.navigate("https://example.com")

        nav = next(e[1] for e in events if e[0] == "browser.navigate")
        assert "duration_ms" in nav
        assert isinstance(nav["duration_ms"], int)
        assert nav["duration_ms"] >= 0


class TestSessionCreatedAuditEvent:
    """emit_created() emits browser.session.created with mode+provider."""

    async def test_session_created_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(
            page=page,
            session_id="s1",
            mode="remote",
            provider="browserbase",
            audit_fn=capture,
        )
        await s.emit_created()

        created = next(e[1] for e in events if e[0] == "browser.session.created")
        assert created["session_id"] == "s1"
        assert created["mode"] == "remote"
        assert created["provider"] == "browserbase"


class TestSessionClosedAuditEvent:
    """close() emits browser.session.closed."""

    async def test_session_closed_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(
            page=page,
            session_id="s2",
            mode="local",
            audit_fn=capture,
        )
        await s.close()

        closed = next(e[1] for e in events if e[0] == "browser.session.closed")
        assert closed["session_id"] == "s2"
        assert closed["mode"] == "local"


class TestClickAuditEvent:
    """click() emits browser.click without element content."""

    async def test_click_emits_audit_event(self) -> None:
        page = _make_page()
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="clk-1", audit_fn=capture)
        await s.click("#submit-btn")

        click_events = [e for e in events if e[0] == "browser.click"]
        assert len(click_events) == 1
        payload = click_events[0][1]
        assert payload["selector"] == "#submit-btn"
        assert payload["session_id"] == "clk-1"
        # Element content must NOT be in the audit payload (LLM02 protection)
        assert "text" not in payload
        assert "content" not in payload
        assert "value" not in payload


class TestExtractAuditEvent:
    """extract() emits browser.extract with size but not content."""

    async def test_extract_emits_audit_event_with_size(self) -> None:
        page = _make_page()
        page.inner_text = AsyncMock(return_value="hello world")
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(name: str, payload: dict[str, Any]) -> None:
            events.append((name, payload))

        s = BrowserSession(page=page, session_id="ext-1", audit_fn=capture)
        await s.extract(".main-content")

        extract_events = [e for e in events if e[0] == "browser.extract"]
        assert len(extract_events) == 1
        payload = extract_events[0][1]
        assert payload["selector"] == ".main-content"
        assert payload["session_id"] == "ext-1"
        assert payload["content_size_bytes"] == len("hello world".encode("utf-8"))
        # Extracted content itself must NOT be in the audit payload
        assert "content" not in payload
        assert "text" not in payload
        assert "value" not in payload


class TestAuditEventOrdering:
    """Audit events appear in the correct order for a typical session."""

    async def test_full_session_event_order(self) -> None:
        """created → navigate → click → extract → closed."""
        page = _make_page()
        page.inner_text = AsyncMock(return_value="abc")
        events: list[str] = []

        async def capture(name: str, _payload: dict[str, Any]) -> None:
            events.append(name)

        s = BrowserSession(page=page, session_id="flow-1", audit_fn=capture)
        await s.emit_created()
        await s.navigate("https://example.com")
        await s.click("#btn")
        await s.extract("h1")
        await s.close()

        assert events[0] == "browser.session.created"
        assert events[-1] == "browser.session.closed"
        assert "browser.navigate" in events
        assert "browser.click" in events
        assert "browser.extract" in events
