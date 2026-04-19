"""Error hierarchy for the web module.

All web errors extend ArcAgentError so they carry a machine-readable code,
component name, and optional details dict for structured audit trails.

Exception names follow the project convention (matching the spec contract)
rather than the PEP-8 "Error suffix" rule — noqa: N818 suppresses the
ruff warning on each class.

Spec: SPEC-018 T4.8
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class WebError(ArcAgentError):
    """Base for all web module failures."""

    _component = "web"


class SearchFailed(WebError):  # noqa: N818
    """Web search provider returned an error or non-2xx response.

    Raised when the upstream search API call fails in a way that is not
    recoverable within a single request (e.g. auth error, rate-limit,
    upstream 5xx).
    """

    def __init__(
        self,
        message: str = "Web search failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="WEB_SEARCH_FAILED", message=message, details=details)


class ExtractFailed(WebError):  # noqa: N818
    """Web extract provider returned an error or non-2xx response.

    Raised when the upstream extraction call fails (DNS error, 4xx/5xx,
    malformed response).
    """

    def __init__(
        self,
        message: str = "Web extraction failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="WEB_EXTRACT_FAILED", message=message, details=details)


class URLNotAllowed(WebError):  # noqa: N818
    """URL rejected by the federal URL allowlist policy.

    At federal tier, every outbound URL must match at least one glob in
    ``WebConfig.url_allowlist``.  Any URL that does not match raises this
    error so the agent cannot silently access un-approved external resources.

    Carrying the ``tier`` in details supports NIST AU-3 audit content.
    """

    def __init__(
        self,
        url: str,
        tier: str = "federal",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"url": url, "tier": tier}
        if details:
            merged.update(details)
        super().__init__(
            code="WEB_URL_NOT_ALLOWED",
            message=f"URL not permitted by allowlist policy (tier={tier}): {url}",
            details=merged,
        )


class ContentTooLarge(WebError):  # noqa: N818
    """Extracted content exceeds ``WebConfig.max_content_bytes``.

    Content is truncated before this error is raised (caller receives
    truncated content) — but this exception is also logged as a warning
    so operators are aware of truncation events.

    This is a warning-level event; the tool still returns truncated content.
    """

    def __init__(
        self,
        url: str,
        actual_bytes: int,
        max_bytes: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {
            "url": url,
            "actual_bytes": actual_bytes,
            "max_bytes": max_bytes,
        }
        if details:
            merged.update(details)
        super().__init__(
            code="WEB_CONTENT_TOO_LARGE",
            message=(
                f"Extracted content truncated: {actual_bytes} bytes "
                f"exceeds max {max_bytes} bytes for {url}"
            ),
            details=merged,
        )


class ProviderConfigMissing(WebError):  # noqa: N818
    """Required provider configuration (API key or endpoint) is absent.

    Raised during provider construction when a required secret cannot be
    resolved via the vault resolver.
    """

    def __init__(
        self,
        provider: str,
        missing_key: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"provider": provider, "missing_key": missing_key}
        if details:
            merged.update(details)
        super().__init__(
            code="WEB_PROVIDER_CONFIG_MISSING",
            message=f"Provider '{provider}' missing required config key: {missing_key}",
            details=merged,
        )


__all__ = [
    "ContentTooLarge",
    "ExtractFailed",
    "ProviderConfigMissing",
    "SearchFailed",
    "URLNotAllowed",
    "WebError",
]
