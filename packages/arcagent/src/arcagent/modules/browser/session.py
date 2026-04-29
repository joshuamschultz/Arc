"""BrowserSession — per-agent Playwright page lifecycle (T4.9).

A ``BrowserSession`` owns one Playwright ``Page`` and exposes the six
tool-surface operations defined in T4.9:

    navigate(url)             → open URL, record audit event
    click(selector)           → click element
    snapshot()                → accessibility tree as structured text
    extract(selector)         → text content of element
    type_text(selector, text) → type into input field
    close()                   → close page and emit session.closed event

Audit events are emitted via the injected ``audit_fn`` callable so that
the session has no direct dependency on ``AgentTelemetry`` and stays
easily testable.

Event names follow the spec:
    browser.session.created   — {session_id, mode, provider}
    browser.session.closed    — {session_id, mode, provider}
    browser.navigate          — {session_id, url, duration_ms}
    browser.click             — {session_id, selector}
    browser.extract           — {session_id, selector, content_size_bytes}

Note on URL logging: Federal compliance requires full URLs in audit
records (NIST AU-3). URLs are logged as plain strings — no hashing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from arcagent.modules.browser.errors import (
    BrowserNotAvailableError,
    ElementNotFoundError,
    NavigationFailedError,
)

_logger = logging.getLogger("arcagent.modules.browser.session")

# Type alias for the audit callback injected by BrowserModule
AuditFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class BrowserSession:
    """Wraps a Playwright Page with tool-surface operations and audit events.

    One session = one Playwright Page. BrowserModule creates sessions on
    demand; operators close them explicitly or they are closed on module
    shutdown.

    Args:
        page:       Playwright ``Page`` instance (or mock in tests).
        session_id: Unique identifier for this session (auto-generated if None).
        mode:       ``"local"`` or ``"remote"`` — recorded in audit events.
        provider:   Provider name — recorded in audit events (e.g. ``"browserbase"``).
        audit_fn:   Async callable ``(event_name, payload) -> None``. Called for
                    every auditable operation. Defaults to a no-op.
    """

    def __init__(
        self,
        page: Any,
        session_id: str | None = None,
        mode: str = "local",
        provider: str | None = None,
        audit_fn: AuditFn | None = None,
    ) -> None:
        self._page = page
        self._session_id = session_id or str(uuid.uuid4())
        self._mode = mode
        self._provider = provider or mode
        self._audit_fn = audit_fn or _noop_audit
        self._closed = False

    @property
    def session_id(self) -> str:
        """Unique identifier for this session."""
        return self._session_id

    @property
    def is_closed(self) -> bool:
        """True if the session has been closed."""
        return self._closed

    async def emit_created(self) -> None:
        """Emit the session.created audit event.

        Called by BrowserModule immediately after creating the session.
        Separated from ``__init__`` because audit emission is async.
        """
        await self._audit(
            "browser.session.created",
            {"session_id": self._session_id, "mode": self._mode, "provider": self._provider},
        )

    async def navigate(self, url: str) -> None:
        """Navigate to ``url`` and emit a ``browser.navigate`` audit event.

        Args:
            url: The full URL to navigate to. Logged verbatim in audit records.

        Raises:
            NavigationFailedError: If Playwright raises during navigation.
        """
        self._require_open()
        start = time.monotonic()
        try:
            await self._page.goto(url)
        except Exception as exc:
            raise NavigationFailedError(url=url, reason=str(exc)) from exc
        duration_ms = int((time.monotonic() - start) * 1000)
        await self._audit(
            "browser.navigate",
            {"session_id": self._session_id, "url": url, "duration_ms": duration_ms},
        )
        _logger.debug(
            "navigate session=%s url=%s duration_ms=%d",
            self._session_id,
            url,
            duration_ms,
        )

    async def click(self, selector: str) -> None:
        """Click the element matched by ``selector``.

        The selector itself is logged but element content is never logged
        (LLM02 / sensitive content protection).

        Args:
            selector: CSS selector or Playwright locator string.

        Raises:
            ElementNotFoundError: If no element matches the selector.
        """
        self._require_open()
        try:
            await self._page.click(selector)
        except Exception as exc:
            raise ElementNotFoundError(
                message=f"Selector '{selector}' not found",
                details={"selector": selector, "reason": str(exc)},
            ) from exc
        await self._audit(
            "browser.click",
            {"session_id": self._session_id, "selector": selector},
        )
        _logger.debug("click session=%s selector=%r", self._session_id, selector)

    async def snapshot(self) -> str:
        """Return the accessibility tree as structured text.

        Uses Playwright's built-in ``page.accessibility.snapshot()`` to
        produce a structured, LLM-friendly representation. Raw HTML is
        never returned — this prevents prompt injection via page content.

        Returns:
            Accessibility tree formatted as indented text.
        """
        self._require_open()
        try:
            tree = await self._page.accessibility.snapshot()
        except Exception as exc:
            _logger.warning("Accessibility snapshot failed: %s", exc)
            return ""
        return _format_ax_tree(tree)

    async def extract(self, selector: str) -> str:
        """Extract text content from the element matched by ``selector``.

        Args:
            selector: CSS selector or Playwright locator string.

        Returns:
            Inner text of the matched element.

        Raises:
            ElementNotFoundError: If no element matches the selector.
        """
        self._require_open()
        try:
            text: str = await self._page.inner_text(selector)
        except Exception as exc:
            raise ElementNotFoundError(
                message=f"Selector '{selector}' not found",
                details={"selector": selector, "reason": str(exc)},
            ) from exc
        content_size = len(text.encode("utf-8"))
        await self._audit(
            "browser.extract",
            {
                "session_id": self._session_id,
                "selector": selector,
                "content_size_bytes": content_size,
            },
        )
        _logger.debug(
            "extract session=%s selector=%r size_bytes=%d",
            self._session_id,
            selector,
            content_size,
        )
        return text

    async def type_text(self, selector: str, text: str) -> None:
        """Type ``text`` into the input matched by ``selector``.

        Input content is intentionally NOT logged to avoid capturing
        credentials or PII via ``browser.type`` audit events.

        Args:
            selector: CSS selector or Playwright locator string.
            text:     Text to type into the element.

        Raises:
            ElementNotFoundError: If no element matches the selector.
        """
        self._require_open()
        try:
            await self._page.fill(selector, text)
        except Exception as exc:
            raise ElementNotFoundError(
                message=f"Selector '{selector}' not found",
                details={"selector": selector, "reason": str(exc)},
            ) from exc
        _logger.debug("type_text session=%s selector=%r", self._session_id, selector)

    async def close(self) -> None:
        """Close the Playwright page and emit the session.closed audit event.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._page.close()
        except Exception:
            _logger.debug("Error closing page for session %s", self._session_id, exc_info=True)
        await self._audit(
            "browser.session.closed",
            {"session_id": self._session_id, "mode": self._mode, "provider": self._provider},
        )
        _logger.info("Browser session closed: %s", self._session_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _audit(self, event: str, payload: dict[str, Any]) -> None:
        """Emit an audit event via the injected audit function."""
        await self._audit_fn(event, payload)

    def _require_open(self) -> None:
        """Raise BrowserNotAvailableError if this session has already been closed."""
        if self._closed:
            raise BrowserNotAvailableError(
                message=f"Session {self._session_id} has been closed",
                details={"session_id": self._session_id},
            )


# ---------------------------------------------------------------------------
# Accessibility tree formatter
# ---------------------------------------------------------------------------


def _format_ax_tree(tree: dict[str, Any] | None, indent: int = 0) -> str:
    """Recursively format an accessibility tree dict as indented text.

    Playwright's ``page.accessibility.snapshot()`` returns a nested dict.
    We render it as readable text so LLMs can navigate without seeing raw HTML.
    """
    if tree is None:
        return ""

    parts: list[str] = []
    prefix = "  " * indent
    role = tree.get("role", "")
    name = tree.get("name", "")
    value = tree.get("value", "")

    line_parts: list[str] = []
    if role:
        line_parts.append(role)
    if name:
        line_parts.append(f'"{name}"')
    if value:
        line_parts.append(f"value={value!r}")

    if line_parts:
        parts.append(prefix + " ".join(line_parts))

    for child in tree.get("children", []):
        child_text = _format_ax_tree(child, indent + 1)
        if child_text:
            parts.append(child_text)

    return "\n".join(parts)


async def _noop_audit(_event: str, _payload: dict[str, Any]) -> None:
    """Default no-op audit function used when no audit_fn is injected."""
    await asyncio.sleep(0)
