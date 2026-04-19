"""URL allowlist policy for the web module.

Implements glob-pattern matching for outbound URL control.

Tier behaviour:
    Federal  — deny by default; every URL must match at least one pattern.
               Empty allowlist = deny all (module startup should reject this
               configuration, but this function is the enforcement point).
    Enterprise — allow all when allowlist is empty; when non-empty, check.
                 Logs a WARNING for cross-org URLs (heuristic: different
                 registered domain than any pattern's domain fragment).
    Personal — no restriction regardless of allowlist contents.

``check_url`` returns ``True`` when the URL is permitted, ``False`` when
denied.  Callers raise ``URLNotAllowed`` on ``False``.

Spec: SPEC-018 T4.8.5
"""

from __future__ import annotations

import fnmatch
import logging
import urllib.parse

_logger = logging.getLogger("arcagent.modules.web.url_policy")


def is_url_allowed(
    url: str,
    *,
    allowlist: list[str],
    tier: str,
) -> bool:
    """Return True if ``url`` is permitted under the given tier policy.

    Args:
        url: The outbound URL to check.
        allowlist: Glob patterns (e.g. ``["https://api.example.com/*"]``).
                   Matched against the full URL string.
        tier: Deployment tier — ``"federal"``, ``"enterprise"``, or
              ``"personal"``.

    Returns:
        True if the URL is allowed, False if it should be denied.
    """
    tier = tier.lower()

    if tier == "personal":
        # Personal tier: no URL restrictions
        return True

    if tier == "federal":
        return _check_allowlist(url, allowlist)

    # Enterprise tier
    if not allowlist:
        # Empty allowlist = allow all at enterprise
        _warn_cross_org_if_needed(url, allowlist)
        return True
    return _check_allowlist(url, allowlist)


def _check_allowlist(url: str, allowlist: list[str]) -> bool:
    """Return True if ``url`` matches any pattern in ``allowlist``."""
    if not allowlist:
        # Empty allowlist at federal/enterprise = deny all
        return False
    return any(fnmatch.fnmatch(url, pattern) for pattern in allowlist)


def _warn_cross_org_if_needed(url: str, allowlist: list[str]) -> None:
    """Warn when a URL looks like it belongs to a different org.

    This is a best-effort heuristic for enterprise tier: if the allowlist
    contains domain fragments, check the URL's registered domain against them.
    When allowlist is empty (allow-all mode) we emit a low-level debug log
    only — no actionable warning needed.
    """
    if not allowlist:
        # allow-all enterprise mode; nothing to compare against
        parsed = urllib.parse.urlparse(url)
        _logger.debug("web.extract enterprise outbound: %s", parsed.netloc)
        return

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    # Check whether the host appears in any allowlist pattern's domain part
    for pattern in allowlist:
        pattern_host = urllib.parse.urlparse(pattern).netloc.lower()
        if pattern_host and pattern_host in host:
            return
    _logger.warning(
        "web.url_policy enterprise cross-org URL detected: %s "
        "(not in any allowlist pattern domain — verify intent)",
        host,
    )


__all__ = ["is_url_allowed"]
