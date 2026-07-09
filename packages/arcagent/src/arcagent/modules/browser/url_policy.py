"""URL security policy helpers for the browser module.

Pure functions shared by the live ``@tool`` navigation surface
(:mod:`arcagent.modules.browser.capabilities`) and the CLI. URL policy
is checked both pre-navigation and post-redirect so a page cannot bounce
the browser onto a blocked domain.
"""

from __future__ import annotations

import fnmatch
from typing import Any
from urllib.parse import urlparse

from arcagent.modules.browser.config import BrowserSecurityConfig
from arcagent.modules.browser.errors import URLBlockedError


def _check_url_policy(url: str, config: BrowserSecurityConfig) -> None:
    """Validate a URL against the security policy.

    Checks scheme blocklist, then allowlist/denylist domain patterns.

    Args:
        url: The URL to validate.
        config: Security config with url_mode and url_patterns.

    Raises:
        URLBlockedError: If the URL violates the security policy.
    """
    parsed = urlparse(url)

    if parsed.scheme in config.blocked_schemes:
        raise URLBlockedError(
            message=f"Scheme '{parsed.scheme}' is blocked by security policy",
            details={"url": url, "scheme": parsed.scheme},
        )

    hostname = parsed.hostname or ""

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
    return fnmatch.fnmatch(hostname, pattern)


async def _get_current_url(cdp: Any) -> str:
    """Get the current page URL via Runtime.evaluate."""
    result = await cdp.send("Runtime", "evaluate", {"expression": "window.location.href"})
    url: str = result.get("result", {}).get("value", "")
    return url
