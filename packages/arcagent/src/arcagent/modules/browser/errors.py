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


class LocalBrowserNotAllowedError(BrowserError):
    """Local (auto-launched) browser execution is forbidden by the tier.

    Federal tier requires attaching to a remote CDP endpoint rather than
    launching a local headless Chrome. Set ``connection.cdp_url`` in
    ``[modules.browser.config]`` to a remote endpoint.
    """

    _DEFAULT_MSG = (
        "Local browser execution is not allowed at this tier; "
        "set connection.cdp_url to a remote CDP endpoint in browser config"
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


class CapabilityDisabledError(BrowserError):
    """A browser capability disabled by config was invoked."""

    def __init__(
        self,
        capability: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="BROWSER_CAPABILITY_DISABLED",
            message=f"Browser capability '{capability}' is disabled by config",
            details={"capability": capability, **(details or {})},
        )
