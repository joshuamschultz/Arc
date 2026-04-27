"""Error hierarchy for the browser module.

All browser-specific errors extend ArcAgentError from core,
keeping the structured error contract (code, component, details)
while living alongside the module they serve.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class BrowserError(ArcAgentError):
    """Base for browser module errors."""

    _component = "browser"


class CDPConnectionError(BrowserError):
    """Chrome launch, WebSocket connect, or protocol error."""

    def __init__(
        self,
        message: str = "CDP connection failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="BROWSER_CDP_CONNECTION", message=message, details=details)


class URLBlockedError(BrowserError):
    """URL rejected by allowlist/denylist or scheme policy."""

    def __init__(
        self,
        message: str = "URL blocked by security policy",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="BROWSER_URL_BLOCKED", message=message, details=details)


class ElementNotFoundError(BrowserError):
    """Ref ID does not resolve to a DOM element."""

    def __init__(
        self,
        message: str = "Element not found",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="BROWSER_ELEMENT_NOT_FOUND", message=message, details=details)


class BrowserTimeoutError(BrowserError):
    """CDP operation exceeded its timeout."""

    def __init__(
        self,
        message: str = "Browser operation timed out",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="BROWSER_TIMEOUT", message=message, details=details)


class BrowserNotAvailableError(BrowserError):
    """Operation attempted on a closed or uninitialized browser session."""

    def __init__(
        self,
        message: str = "Browser session is not available",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="BROWSER_NOT_AVAILABLE", message=message, details=details)


class NavigationFailedError(BrowserError):
    """Page navigation failed (e.g., network error, HTTP error, timeout)."""

    def __init__(
        self,
        url: str = "",
        reason: str = "Navigation failed",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        effective_message = (
            message or f"Navigation failed: {reason}" if reason else "Navigation failed"
        )
        merged = {"url": url, "reason": reason, **(details or {})}
        super().__init__(
            code="BROWSER_NAVIGATION_FAILED",
            message=effective_message,
            details=merged,
        )


class LocalBrowserNotAllowedError(BrowserError):
    """Local browser execution is not permitted by the current policy.

    Federal tier requires remote browser providers (e.g., Browserbase).
    Configure ``mode = "remote"`` in ``[modules.browser.config]``.
    """

    _DEFAULT_MSG = (
        "Local browser execution is not allowed; "
        "set remote_provider and endpoint in browser config"
    )

    def __init__(
        self,
        tier: str = "unknown",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"tier": tier, **(details or {})}
        super().__init__(
            code="BROWSER_LOCAL_NOT_ALLOWED",
            message=message or self._DEFAULT_MSG,
            details=merged,
        )


class RemoteProviderError(BrowserError):
    """Remote browser provider (e.g., Browserbase) returned an error."""

    def __init__(
        self,
        provider: str = "",
        message: str = "Remote browser provider error",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"provider": provider, **(details or {})} if provider else (details or {})
        super().__init__(code="BROWSER_REMOTE_PROVIDER_ERROR", message=message, details=merged)
