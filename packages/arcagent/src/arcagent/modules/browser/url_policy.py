"""URL security policy helpers for the browser module.

Pure functions shared by the live ``@tool`` navigation surface
(:mod:`arcagent.modules.browser.capabilities`) and the CLI. URL policy
is checked both pre-navigation and post-redirect so a page cannot bounce
the browser onto a blocked domain.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from arcagent.modules.browser.config import BrowserSecurityConfig
from arcagent.modules.browser.errors import URLBlockedError
from arcagent.utils.url_security import UnsafeURLError, validate_http_url


def _check_url_policy(
    url: str,
    config: BrowserSecurityConfig,
    *,
    resolve: bool = False,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> None:
    """Validate a URL against the security policy.

    Checks scheme blocklist, then allowlist/denylist domain patterns.

    Args:
        url: The URL to validate.
        config: Security config with url_mode and url_patterns.

    Raises:
        URLBlockedError: If the URL violates the security policy.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise URLBlockedError(
            message=f"Scheme '{scheme}' is blocked by security policy",
            details={"url": url, "scheme": scheme},
        )
    try:
        validated = validate_http_url(
            url,
            resolve=resolve,
            **({"resolver": resolver} if resolver is not None else {}),
        )
    except UnsafeURLError as exc:
        raise URLBlockedError(
            message=f"URL is blocked by security policy: {exc}", details={"url": url}
        ) from exc

    hostname = validated.hostname

    if config.url_mode == "allowlist":
        if not any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(
                message=f"Domain '{hostname}' not in allowlist",
                details={"url": url, "hostname": hostname, "mode": "allowlist"},
            )
    else:  # denylist
        if any(_match_pattern(hostname, p) for p in config.url_patterns):
            raise URLBlockedError(
                message=f"Domain '{hostname}' is blocked by denylist",
                details={"url": url, "hostname": hostname, "mode": "denylist"},
            )


def _match_pattern(hostname: str, pattern: str) -> bool:
    """Match a hostname against a glob-style domain pattern.

    Supports patterns like ``*.example.com`` and ``example.com``.
    """
    hostname = hostname.rstrip(".").lower()
    pattern = pattern.rstrip(".").lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname != suffix and hostname.endswith(f".{suffix}")
    return hostname == pattern


async def _get_current_url(cdp: Any) -> str:
    """Get the current page URL via Runtime.evaluate."""
    result = await cdp.send("Runtime", "evaluate", {"expression": "window.location.href"})
    url: str = result.get("result", {}).get("value", "")
    return url
